"""Authenticated loopback policy proxies for trusted provider SDK clients."""

from __future__ import annotations

import asyncio
import atexit
import ipaddress
import threading
from contextlib import suppress
from dataclasses import dataclass, field

import httpx

from crew.browser.security import (
    BrowserNetworkPolicy,
    LoopbackPolicyProxy,
)
from crew.browser.types import BrowserConfig
from crew.security.outbound import OutboundContext, OutboundDenied, OutboundPolicy
from crew.tools.redact import argv_contains_sensitive_value

_MAX_PROVIDER_ORIGINS = 32
_START_TIMEOUT_SECONDS = 5.0
_REGISTRY_LOCK = threading.Lock()
_RUNTIMES: dict[
    tuple[tuple[str, str, int], bool, OutboundContext | None],
    "_ProviderProxyRuntime",
] = {}
_CANONICALIZER = OutboundPolicy()


class ProviderProxyUnavailable(RuntimeError):
    """A provider cannot run without its mandatory policy proxy."""


@dataclass(frozen=True, slots=True)
class ProviderProxyConfig:
    """Structured proxy endpoint that never embeds credentials in a URL."""

    endpoint_url: str
    username: str
    password: str = field(repr=False)
    origin: tuple[str, str, int] | None = field(default=None, repr=False, compare=False)
    _credential_issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            parsed, target = _CANONICALIZER.canonicalize_url(
                self.endpoint_url,
                allowed_schemes=frozenset({"http"}),
            )
            address = ipaddress.ip_address(target.host)
        except (OutboundDenied, ValueError) as exc:
            raise ProviderProxyUnavailable("provider proxy endpoint is invalid") from exc
        if (
            not address.is_loopback
            or target.path != "/"
            or target.query
            or parsed.fragment
            or target.method != "GET"
            or not isinstance(self.username, str)
            or not isinstance(self.password, str)
            or not self.username
            or not self.password
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in self.username + self.password)
        ):
            raise ProviderProxyUnavailable("provider proxy endpoint is invalid")
        if self.origin is None:
            raise ProviderProxyUnavailable("provider proxy origin is unavailable")
        scheme, host, port = self.origin
        if (
            not isinstance(scheme, str)
            or not isinstance(host, str)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ProviderProxyUnavailable("provider origin is invalid")
        origin_host = f"[{host}]" if ":" in host else host
        try:
            canonical_origin = _CANONICALIZER.canonicalize_url(
                f"{scheme}://{origin_host}:{port}/",
                allowed_schemes=frozenset({"http", "https"}),
            )[1]
        except OutboundDenied as exc:
            raise ProviderProxyUnavailable("provider origin is invalid") from exc
        if (canonical_origin.scheme, canonical_origin.host, canonical_origin.port) != self.origin:
            raise ProviderProxyUnavailable("provider origin is invalid")

    def validate_request(self, url: str, *, method: str) -> None:
        try:
            _parsed, target = _CANONICALIZER.canonicalize_url(
                url,
                method=method,
                allowed_schemes=frozenset({"http", "https"}),
            )
        except OutboundDenied as exc:
            raise ProviderProxyUnavailable("provider request is invalid") from exc
        if (target.scheme, target.host, target.port) != self.origin:
            raise ProviderProxyUnavailable("provider request origin mismatch")

    @property
    def credentials(self) -> tuple[str, str]:
        return self.username, self.password

    def httpx_proxy(self, context: OutboundContext | None = None):
        if context is None:
            candidate = _provider_context_from_runtime()
            try:
                candidate.require_complete()
            except OutboundDenied:
                candidate = None
            context = candidate
        credentials = self.credentials
        if context is not None:
            issuer = self._credential_issuer
            if not callable(issuer):
                raise ProviderProxyUnavailable("provider context unavailable")
            credentials = issuer(context)
        return httpx.Proxy(self.endpoint_url, auth=credentials)

    def httpx_transport(self, *, verify: object = True):
        """Build a transport that signs the current execution on each request."""
        return _ContextBoundAsyncTransport(self, verify=verify)


class _ClosingAsyncStream(httpx.AsyncByteStream):
    def __init__(self, stream: object, transport: object) -> None:
        self._stream = stream
        self._transport = transport

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            close_stream = getattr(self._stream, "aclose", None)
            if callable(close_stream):
                await close_stream()
        finally:
            close_transport = getattr(self._transport, "aclose", None)
            if callable(close_transport):
                await close_transport()


class _ContextBoundAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, config: ProviderProxyConfig, *, verify: object) -> None:
        self._config = config
        self._verify = verify

    async def handle_async_request(self, request):
        context = _provider_context_from_runtime()
        try:
            context.require_complete()
        except OutboundDenied as exc:
            raise ProviderProxyUnavailable("provider context required") from exc
        self._config.validate_request(str(request.url), method=str(request.method))
        proxy = self._config.httpx_proxy(context=context)
        transport = httpx.AsyncHTTPTransport(
            verify=self._verify,
            trust_env=False,
            proxy=proxy,
        )
        try:
            response = await transport.handle_async_request(request)
        except BaseException:
            await transport.aclose()
            raise
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_ClosingAsyncStream(response.stream, transport),
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()


@dataclass
class _ProviderProxyRuntime:
    origin: tuple[str, str, int]
    context: OutboundContext | None = None
    allow_private: bool = False
    ready: threading.Event = field(default_factory=threading.Event)
    stopping: threading.Event = field(default_factory=threading.Event)
    config: ProviderProxyConfig | None = None
    error: str = ""
    loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    proxy: LoopbackPolicyProxy | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def start(self) -> ProviderProxyConfig:
        self.thread = threading.Thread(
            target=self._run,
            name="crew-provider-policy-proxy",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(_START_TIMEOUT_SECONDS):
            self.close()
            raise ProviderProxyUnavailable("provider policy proxy unavailable")
        if self.config is None:
            raise ProviderProxyUnavailable("provider policy proxy unavailable")
        return self.config

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        _scheme, host, _port = self.origin
        policy = BrowserNetworkPolicy(
            BrowserConfig(
                max_transfer_bytes=100_000_000,
                allowed_private_hosts=[host] if self.allow_private else [],
            ),
            owner=self.context.owner if self.context is not None else "provider",
            allowed_origins=(self.origin,),
            default_allow_public=False,
            proxy_context=self.context,
        )
        proxy = LoopbackPolicyProxy(policy)
        self.proxy = proxy
        try:
            loop.run_until_complete(proxy.start())
            username, password = proxy.credentials
            self.config = ProviderProxyConfig(
                endpoint_url=proxy.endpoint_url,
                username=username,
                password=password,
                origin=self.origin,
                _credential_issuer=proxy.credentials_for,
            )
        except BaseException as exc:
            self.error = type(exc).__name__
            self.ready.set()
            with suppress(BaseException):
                loop.run_until_complete(proxy.aclose())
            loop.close()
            return
        self.ready.set()
        if self.stopping.is_set():
            with suppress(BaseException):
                loop.run_until_complete(proxy.aclose())
            loop.close()
            return
        try:
            loop.run_forever()
        finally:
            with suppress(BaseException):
                loop.run_until_complete(proxy.aclose())
            loop.close()

    def close(self) -> None:
        self.stopping.set()
        loop = self.loop
        proxy = self.proxy
        if loop is not None and loop.is_running():
            if proxy is not None:
                with suppress(BaseException):
                    asyncio.run_coroutine_threadsafe(
                        proxy.aclose(),
                        loop,
                    ).result(timeout=_START_TIMEOUT_SECONDS)
            with suppress(BaseException):
                loop.call_soon_threadsafe(loop.stop)
        thread = self.thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=_START_TIMEOUT_SECONDS)


def provider_policy_proxy(
    url: str,
    *,
    allow_private: bool = False,
    context: OutboundContext | None = None,
) -> ProviderProxyConfig:
    """Return a mandatory proxy restricted to one explicitly configured origin."""
    if argv_contains_sensitive_value((url,)):
        raise ProviderProxyUnavailable(
            "provider endpoint must not contain credentials"
        )
    try:
        _parsed, target = _CANONICALIZER.canonicalize_url(
            url,
            allowed_schemes=frozenset({"http", "https"}),
        )
    except OutboundDenied as exc:
        raise ProviderProxyUnavailable("provider endpoint is invalid") from exc
    if target.scheme == "http":
        try:
            address = ipaddress.ip_address(target.host)
        except ValueError:
            private_http = target.host == "localhost" or target.host.endswith(
                ".localhost"
            )
        else:
            mapped = (
                address.ipv4_mapped
                if isinstance(address, ipaddress.IPv6Address)
                else None
            )
            private_http = bool(
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or (mapped and (mapped.is_private or mapped.is_loopback))
            )
        if not allow_private or not private_http:
            raise ProviderProxyUnavailable(
                "provider endpoint is invalid: HTTPS is required"
            )
    origin = (target.scheme, target.host, target.port)
    bound_context = context
    if bound_context is not None:
        try:
            bound_context.require_complete()
        except OutboundDenied as exc:
            raise ProviderProxyUnavailable("provider context required") from exc
    registry_key = (origin, bool(allow_private), bound_context)
    with _REGISTRY_LOCK:
        runtime = _RUNTIMES.get(registry_key)
        if runtime is not None:
            if runtime.config is None:
                raise ProviderProxyUnavailable("provider policy proxy unavailable")
            return runtime.config
        if len(_RUNTIMES) >= _MAX_PROVIDER_ORIGINS:
            raise ProviderProxyUnavailable("provider proxy origin limit reached")
        runtime = _ProviderProxyRuntime(
            origin,
            context=bound_context,
            allow_private=bool(allow_private),
        )
        _RUNTIMES[registry_key] = runtime
        try:
            return runtime.start()
        except BaseException:
            _RUNTIMES.pop(registry_key, None)
            raise


def _close_provider_proxies() -> None:
    with _REGISTRY_LOCK:
        runtimes = tuple(_RUNTIMES.values())
        _RUNTIMES.clear()
    for runtime in runtimes:
        runtime.close()


atexit.register(_close_provider_proxies)


def _provider_context_from_runtime() -> OutboundContext:
    from crew.core.runctx import (
        current_owner_account_id,
        current_request_id,
        current_session_id,
        current_task_runtime_id,
        current_workspace_id,
    )

    return OutboundContext(
        owner=current_owner_account_id.get(),
        session=current_session_id.get(),
        task=current_task_runtime_id.get(),
        request=current_request_id.get(),
        source="provider_proxy",
        environment=current_workspace_id.get(),
    )
