"""Provider 与 SingleAgent 的资源所有权生命周期测试。"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
import httpx
import pytest

from crew.agent.runtime import SingleAgent
from crew.app import AgentManager, build_app
from crew.core.envelope import Envelope
from crew.core.mocks import FakeProvider, InMemorySessionStore, NullMemory
from crew.plugins.manager import PluginManager
from crew.providers.anthropic_provider import AnthropicProvider
from crew.providers.openai_provider import OpenAIProvider
from crew.security.outbound import OutboundContext
from crew.security.provider_proxy import ProviderProxyUnavailable, provider_policy_proxy
from crew.tools.registry import Registry
from crew.state.config import Config


class _AsyncCloseClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.close_calls = 0
        self.fail = fail

    async def close(self) -> None:
        self.close_calls += 1
        if self.fail:
            raise RuntimeError("close failed")


class _AsyncAcloseClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _ClosableProvider(FakeProvider):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.close_calls = 0
        self.fail = fail

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.fail:
            raise RuntimeError("provider close failed")


@pytest.mark.asyncio
async def test_openai_provider_aclose_is_concurrently_idempotent(monkeypatch):
    import openai

    _ = openai.AsyncOpenAI
    client = _AsyncCloseClient()
    http_client_kwargs: dict[str, object] = {}

    def fake_http_client(**kwargs):
        http_client_kwargs.update(kwargs)
        return _AsyncAcloseClient()

    monkeypatch.setattr("crew.providers.openai_provider.httpx.AsyncClient", fake_http_client)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAIProvider(api_key="sk-test")

    assert http_client_kwargs["trust_env"] is False
    assert http_client_kwargs["follow_redirects"] is False
    transport = http_client_kwargs["transport"]
    assert isinstance(transport, httpx.AsyncBaseTransport)
    assert "proxy" not in http_client_kwargs
    config = transport._config
    assert config.username == "crew"
    assert config.endpoint_url.startswith("http://127.0.0.1:")
    assert config.password not in repr(transport)
    await asyncio.gather(provider.aclose(), provider.aclose(), provider.aclose())

    assert client.close_calls == 1
    assert provider._client is None
    assert provider._secret_values == ()


@pytest.mark.asyncio
async def test_anthropic_provider_aclose_is_concurrently_idempotent(monkeypatch):
    client = _AsyncAcloseClient()
    http_client_kwargs: dict[str, object] = {}

    def fake_http_client(**kwargs):
        http_client_kwargs.update(kwargs)
        return client

    monkeypatch.setattr("httpx.AsyncClient", fake_http_client)
    provider = AnthropicProvider(api_key="sk-test")

    assert http_client_kwargs["trust_env"] is False
    assert http_client_kwargs["follow_redirects"] is False
    transport = http_client_kwargs["transport"]
    assert isinstance(transport, httpx.AsyncBaseTransport)
    assert "proxy" not in http_client_kwargs
    config = transport._config
    assert config.username == "crew"
    assert config.endpoint_url.startswith("http://127.0.0.1:")
    assert config.password not in repr(transport)
    await asyncio.gather(provider.aclose(), provider.aclose(), provider.aclose())

    assert client.close_calls == 1
    assert provider._client is None
    assert provider._headers == {}
    assert provider._secret_values == ()


@pytest.mark.asyncio
async def test_provider_proxy_requires_explicit_private_origin_configuration():
    reached = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        reached.set()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
            b"Connection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    endpoint = f"http://127.0.0.1:{port}/v1"
    try:
        with pytest.raises(ProviderProxyUnavailable):
            provider_policy_proxy(endpoint)
        assert not reached.is_set()

        allowed_proxy = provider_policy_proxy(endpoint, allow_private=True)
        async with httpx.AsyncClient(
            proxy=allowed_proxy.httpx_proxy(),
            trust_env=False,
            timeout=2,
        ) as client:
            denied = await client.get(endpoint)
        assert denied.status_code == 403
        assert not reached.is_set()

        context = OutboundContext(
            owner="owner-provider",
            session="session-provider",
            task="task-provider",
            request="request-provider",
            source="test",
            environment="test-environment",
        )
        context_proxy = provider_policy_proxy(
            endpoint,
            allow_private=True,
            context=context,
        )
        async with httpx.AsyncClient(
            proxy=context_proxy.httpx_proxy(),
            trust_env=False,
            timeout=2,
        ) as client:
            allowed = await client.get(endpoint)
        assert allowed.status_code == 200
        assert allowed.text == "ok"
        assert reached.is_set()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@api.example.test/v1",
        "https://api.example.test/v1?api_key=query-secret",
        "https://api.example.test/v1#access_token=fragment-secret",
    ],
)
def test_provider_proxy_rejects_credentials_in_endpoint_url(endpoint):
    with pytest.raises(
        ProviderProxyUnavailable,
        match="must not contain credentials",
    ):
        provider_policy_proxy(endpoint)


@pytest.mark.asyncio
async def test_single_agent_closes_owned_providers_once_by_identity_and_skips_borrowed():
    borrowed = _ClosableProvider()
    owned_primary = _ClosableProvider()
    owned_fallback = _ClosableProvider()
    agent = SingleAgent(
        provider=borrowed,
        registry=Registry(),
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=PluginManager(),
        owned_providers=[owned_primary, owned_fallback, owned_primary],
    )

    await asyncio.gather(agent.aclose(), agent.aclose())

    assert borrowed.close_calls == 0
    assert owned_primary.close_calls == 1
    assert owned_fallback.close_calls == 1


@pytest.mark.asyncio
async def test_single_agent_close_failure_does_not_block_other_owned_providers():
    failed = _ClosableProvider(fail=True)
    healthy = _ClosableProvider()
    agent = SingleAgent(
        provider=FakeProvider(),
        registry=Registry(),
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=PluginManager(),
        owned_providers=[failed, healthy],
    )

    with pytest.raises(RuntimeError, match="Provider close failed"):
        await agent.aclose()
    await agent.aclose()

    assert failed.close_calls == 1
    assert healthy.close_calls == 1


def test_provider_errors_redact_exact_nonstandard_credentials(monkeypatch) -> None:
    openai_client = _AsyncCloseClient()
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: openai_client)
    openai_secret = "custom-openai-credential-value"
    openai_provider = OpenAIProvider(api_key=openai_secret)
    assert openai_secret not in openai_provider._safe_error(
        RuntimeError(f"upstream echoed {openai_secret}")
    )

    anthropic_client = _AsyncAcloseClient()
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: anthropic_client)
    anthropic_secret = "custom-anthropic-credential-value"
    anthropic_provider = AnthropicProvider(api_key=anthropic_secret)
    assert anthropic_secret not in anthropic_provider._safe_error(
        RuntimeError(f"upstream echoed {anthropic_secret}")
    )


@pytest.mark.asyncio
async def test_closed_single_agent_rejects_late_run_reference():
    agent = SingleAgent(
        provider=FakeProvider(),
        registry=Registry(),
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=PluginManager(),
    )
    await agent.aclose()

    with pytest.raises(RuntimeError, match="已关闭"):
        await anext(agent.run(Envelope.of("late", session_id="s-late")))


@pytest.mark.asyncio
async def test_single_agent_joins_title_borrowers_before_closing_provider():
    provider = _ClosableProvider()
    cancelled = asyncio.Event()

    async def title_job() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    agent = SingleAgent(
        provider=provider,
        registry=Registry(),
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=PluginManager(),
        owned_providers=[provider],
    )
    title_task = asyncio.create_task(title_job())
    await asyncio.sleep(0)
    agent._title_tasks.add(title_task)

    await agent.aclose()

    assert cancelled.is_set()
    assert title_task.done()
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_app_shutdown_closes_agent_owned_before_global_provider_and_is_idempotent(tmp_path):
    events: list[str] = []

    class OrderedAgent:
        async def aclose(self) -> None:
            events.append("agent")

    class OrderedProvider(FakeProvider):
        async def aclose(self) -> None:
            events.append("provider")

    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        crew_home=str(tmp_path / ".crew"),
    )
    app = build_app(config=cfg, enable_team=False)
    app.cron_service = None
    app.mcp_manager = None
    app.provider = OrderedProvider()
    app.agents = AgentManager(OrderedAgent)
    app.agents.get("s1")

    await app.shutdown()
    await app.shutdown()

    assert events == ["agent", "provider"]


@pytest.mark.asyncio
async def test_app_shutdown_deadline_bounds_cancellation_resistant_provider_retirement(tmp_path):
    class HangingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = asyncio.Event()

        async def aclose(self) -> None:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Model an SDK close that needs process teardown and suppresses cancellation.
                await self.release.wait()
            finally:
                self.closed.set()

    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        crew_home=str(tmp_path / ".crew"),
    )
    app = build_app(config=cfg, enable_team=False)
    app.cron_service = None
    app.mcp_manager = None
    hanging = HangingProvider()
    app._schedule_provider_retirement(hanging)
    await asyncio.wait_for(hanging.started.wait(), timeout=0.2)

    try:
        started_at = asyncio.get_running_loop().time()
        await app.shutdown(timeout=0.02)
        elapsed = asyncio.get_running_loop().time() - started_at

        assert elapsed < 0.1
        assert app._shutdown_complete is True
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            app.active_owner.current()
    finally:
        hanging.release.set()
        await asyncio.wait_for(hanging.closed.wait(), timeout=0.2)
        if not app._shutdown_complete:
            await app.shutdown()


@pytest.mark.asyncio
async def test_external_app_shutdown_cancellation_propagates_to_provider_cleanup(tmp_path):
    class CancelAwareProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = asyncio.Event()

        async def aclose(self) -> None:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            finally:
                self.closed.set()

    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        crew_home=str(tmp_path / ".crew"),
    )
    app = build_app(config=cfg, enable_team=False)
    app.cron_service = None
    app.mcp_manager = None
    provider = CancelAwareProvider()
    app.provider = provider
    shutdown = asyncio.create_task(app.shutdown(timeout=1.0))
    await asyncio.wait_for(provider.started.wait(), timeout=0.2)

    try:
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        await asyncio.wait_for(provider.cancelled.wait(), timeout=0.2)
        assert app._shutdown_complete is False
    finally:
        provider.release.set()
        await asyncio.wait_for(provider.closed.wait(), timeout=0.2)
        if not app._shutdown_complete:
            app.provider = _ClosableProvider()
            await app.shutdown()


@pytest.mark.asyncio
async def test_replaced_global_provider_waits_for_pre_switch_consumer(tmp_path):
    gate = asyncio.Event()
    provider = _ClosableProvider()
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        crew_home=str(tmp_path / ".crew"),
    )
    app = build_app(config=cfg, enable_team=False)
    app.cron_service = None
    app.mcp_manager = None

    async def consumer() -> None:
        await gate.wait()

    blocker = asyncio.create_task(consumer())
    app.dispatcher.active_tasks_snapshot = lambda: {blocker}
    app._schedule_provider_retirement(provider)
    await asyncio.sleep(0)
    assert provider.close_calls == 0

    gate.set()
    await blocker
    await app._drain_provider_retirements()
    assert provider.close_calls == 1
    await app.shutdown()


@pytest.mark.asyncio
async def test_use_model_installs_new_provider_before_retiring_old_one(tmp_path, monkeypatch):
    import crew.app as app_module

    old_provider = _ClosableProvider()
    new_provider = _ClosableProvider()
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        crew_home=str(tmp_path / ".crew"),
    )
    app = build_app(config=cfg, enable_team=False)
    app.cron_service = None
    app.mcp_manager = None
    app.provider = old_provider
    profile = SimpleNamespace(id="next", model="next-model", base_url="")
    monkeypatch.setattr(app.config, "activate_model", lambda _model_id: profile)
    monkeypatch.setattr(app_module, "build_provider", lambda _cfg: new_provider)

    selected = app.use_model("next")

    assert selected is profile
    assert app.provider is new_provider
    await app._drain_provider_retirements()
    assert old_provider.close_calls == 1
    assert new_provider.close_calls == 0
    await app.shutdown()
    assert new_provider.close_calls == 1
