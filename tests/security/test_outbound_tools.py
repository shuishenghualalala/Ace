from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.core.errors import ToolError
from crew.security import outbound
from crew.security.outbound import parse_public_http_target
from crew.tools import web_fetch_cache, web_tools
from crew.tools.security_guard import authorize_configured_mcp_call, authorize_exec_tool
from crew.tools.web_fetch_cache import CachedFetch


@pytest.fixture(autouse=True)
def _clean_fetch_cache() -> None:
    # web_extract 用例共用 https://example.com，不隔离会跨用例假命中
    web_fetch_cache.clear_web_fetch_cache()
    yield
    web_fetch_cache.clear_web_fetch_cache()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:password@example.com/",
        "https://example.com/\r\nX-Test: injected",
    ],
)
def test_public_http_target_rejects_local_and_ambiguous_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_public_http_target(url)


def test_public_http_target_normalizes_exact_host_port_protocol() -> None:
    target = parse_public_http_target("https://EXAMPLE.com./docs")
    assert (target.host, target.port, target.protocol) == ("example.com", 443, "https")


@pytest.mark.asyncio
async def test_remote_mcp_authorization_binds_endpoint_and_complete_arguments(tmp_path: Path) -> None:
    from crew.security.approvals import ApprovalDecision

    class Service:
        def __init__(self) -> None:
            self.calls = []

        def authorize_exec_action(self, context, action, **kwargs):
            self.calls.append((context, action, kwargs))
            if len(self.calls) == 1:
                return SimpleNamespace(allowed=False, request={"request_id": "mcp-approval"})
            return SimpleNamespace(allowed=True, request=None)

        async def await_decision(self, request_id):
            assert request_id == "mcp-approval"
            return SimpleNamespace(decision=ApprovalDecision.ONCE)

    context = SimpleNamespace(workspace_root=tmp_path)
    service = Service()
    await authorize_configured_mcp_call(
        "http://127.0.0.1:8765/mcp",
        tool_name="local__mutate",
        args={"path": "/tmp/a", "value": 2},
        security_service=service,
        security_context=context,
    )

    assert len(service.calls) == 2
    first_action = service.calls[0][1]
    second_action = service.calls[1][1]
    assert first_action == second_action
    assert first_action.argv[:2] == ("mcp-call", "local__mutate")
    additional = service.calls[0][2]["additional_permissions"]
    assert additional.network[0].host == "127.0.0.1"
    assert additional.network[0].allow_private is True
    assert service.calls[0][2]["requires_approval"] is True


@pytest.mark.asyncio
async def test_site_build_authorization_waits_and_rechecks_exact_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    context = SimpleNamespace()

    class Service:
        def __init__(self) -> None:
            self.calls = []

        def authorize_exec_action(self, received_context, action, **kwargs):
            self.calls.append((received_context, action, kwargs))
            if len(self.calls) == 1:
                return SimpleNamespace(allowed=False, request={"request_id": "approval"})
            return SimpleNamespace(allowed=True, request=None)

        async def await_decision(self, request_id):
            assert request_id == "approval"
            from crew.security.approvals import ApprovalDecision

            return SimpleNamespace(decision=ApprovalDecision.ONCE)

    service = Service()
    monkeypatch.setattr("crew.tools.security_guard.build_security_context", lambda _store: context)

    await authorize_exec_tool(
        ("/usr/bin/node", "/runtime/npm-cli.js", "run", "build"),
        cwd=tmp_path,
        tool_name="publish_site",
        workspace_store=object(),
        security_service=service,
        preview="npm run build",
    )

    assert len(service.calls) == 2
    assert service.calls[0][1] == service.calls[1][1]
    assert service.calls[0][1].cwd == str(tmp_path.resolve())
    assert service.calls[0][2]["requires_approval"] is False
    assert service.calls[1][2]["requires_approval"] is True


@pytest.mark.asyncio
async def test_web_extract_authorizes_before_cache_and_fetch(monkeypatch) -> None:
    order: list[str] = []

    async def allow(_url: str, **_kwargs) -> None:
        order.append("authorize")

    def cache_lookup(_key: str):
        order.append("cache_lookup")
        return None

    def fetch(url: str, *_args) -> tuple[str, str]:
        order.append("fetch")
        return url, "<html><title>Safe</title><body>Body</body></html>"

    monkeypatch.setattr(web_tools, "authorize_network_tool", allow)
    monkeypatch.setattr(web_fetch_cache, "get_cached_fetch", cache_lookup)
    monkeypatch.setattr(web_tools, "_fetch_url", fetch)

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    # 缓存门 authorize → 查缓存（未命中）→ _authorized_fetch 首跳 authorize → fetch
    assert order == ["authorize", "cache_lookup", "authorize", "fetch"]
    assert payload["title"] == "Safe"
    assert payload["text"].endswith("\n\nBody")


@pytest.mark.asyncio
async def test_web_extract_authorizes_cross_host_redirect_before_following(monkeypatch) -> None:
    authorized: list[str] = []
    redirected = "https://cdn.example.org/article"

    async def allow(url: str, **_kwargs) -> None:
        authorized.append(url)

    def fetch(url: str, _timeout: float, allowed: set[tuple[str, int, str]]):
        if ("cdn.example.org", 443, "https") not in allowed:
            raise web_tools.PublicRedirectApprovalRequired(redirected)
        return redirected, "<title>Redirected</title>Body"

    monkeypatch.setattr(web_tools, "authorize_network_tool", allow)
    monkeypatch.setattr(web_tools, "_fetch_url", fetch)

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com/a"}))

    # 缓存门的第一次 + _authorized_fetch 首跳的第二次 + 重定向目标
    assert authorized == ["https://example.com/a", "https://example.com/a", redirected]
    assert payload["url"] == redirected


@pytest.mark.asyncio
async def test_web_extract_cache_hit_still_authorizes(monkeypatch) -> None:
    web_fetch_cache.put_cached_fetch(
        "https://example.com",
        CachedFetch(
            final_url="https://example.com",
            title="Cached",
            markdown="# Cached Body",
            source_truncated=False,
        ),
    )
    authorized: list[str] = []

    async def allow(url: str, **_kwargs) -> None:
        authorized.append(url)

    def fetch(url: str, *_args) -> tuple[str, str]:
        raise AssertionError("cache hit must not touch the network")

    monkeypatch.setattr(web_tools, "authorize_network_tool", allow)
    monkeypatch.setattr(web_tools, "_fetch_url", fetch)

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert authorized == ["https://example.com"]  # 缓存命中也必须先过授权
    assert payload["cached"] is True
    assert "# Cached Body" in payload["text"]


@pytest.mark.asyncio
async def test_web_extract_denied_target_never_reads_cache(monkeypatch) -> None:
    cache_reads: list[str] = []

    def cache_lookup(key: str):
        cache_reads.append(key)
        return None

    async def deny(_url: str, **_kwargs) -> None:
        raise ToolError("联网请求已被安全策略拒绝")

    monkeypatch.setattr(web_tools, "authorize_network_tool", deny)
    monkeypatch.setattr(web_fetch_cache, "get_cached_fetch", cache_lookup)

    with pytest.raises(ToolError):
        await web_tools.handle_web_extract({"url": "https://example.com"})

    assert cache_reads == []  # 授权拒绝时连缓存都不该碰


# ---------------------------------------------------------------------------
# 上游代理解析与隧道
# ---------------------------------------------------------------------------

def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    outbound.set_network_defaults(upstream_proxy="")


def test_resolve_upstream_proxy_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    proxy = outbound.resolve_upstream_proxy("https")
    assert proxy is not None
    assert (proxy.scheme, proxy.host, proxy.port) == ("http", "127.0.0.1", 7890)


def test_resolve_upstream_proxy_returns_none_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    assert outbound.resolve_upstream_proxy("https") is None


def test_resolve_upstream_proxy_explicit_beats_config_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://env.proxy:8888")
    outbound.set_network_defaults(upstream_proxy="http://cfg.proxy:9999")
    proxy = outbound.resolve_upstream_proxy("https", explicit="http://explicit.proxy:7777")
    assert proxy is not None and proxy.host == "explicit.proxy"
    try:
        proxy = outbound.resolve_upstream_proxy("https")
        assert proxy is not None and proxy.host == "cfg.proxy"
    finally:
        outbound.set_network_defaults(upstream_proxy="")


def test_resolve_upstream_proxy_config_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://env.proxy:8888")
    outbound.set_network_defaults(upstream_proxy="http://cfg.proxy:9999")
    try:
        proxy = outbound.resolve_upstream_proxy("https")
        assert proxy is not None and proxy.host == "cfg.proxy"
    finally:
        outbound.set_network_defaults(upstream_proxy="")


@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks5h"])
def test_resolve_upstream_proxy_supports_all_schemes(
    scheme: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)
    proxy = outbound.resolve_upstream_proxy("https", explicit=f"{scheme}://proxy.local:1234")
    assert proxy is not None
    assert (proxy.scheme, proxy.host, proxy.port) == (scheme, "proxy.local", 1234)


def test_resolve_upstream_proxy_rejects_unsupported_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)
    with pytest.raises(ValueError):
        outbound.resolve_upstream_proxy("https", explicit="ftp://proxy.local")


def test_resolve_upstream_proxy_keeps_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    proxy = outbound.resolve_upstream_proxy("https", explicit="http://user:pass@proxy.local:8080")
    assert proxy is not None
    assert (proxy.username, proxy.password) == ("user", "pass")


def test_http_tunnel_sends_connect_with_authority_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECT authority 必须带端口，部分代理对省略端口的 CONNECT 响应异常。"""
    sent: list[bytes] = []

    class FakeSocket:
        def __init__(self) -> None:
            self.timeout = None

        def sendall(self, data: bytes) -> None:
            sent.append(data)

        def recv(self, _n: int) -> bytes:
            return b"HTTP/1.1 200 Connection established\r\n\r\n"

        def settimeout(self, value) -> None:
            self.timeout = value

        def close(self) -> None:
            pass

    fake = FakeSocket()
    monkeypatch.setattr(outbound.socket, "create_connection", lambda *_a, **_k: fake)
    proxy = outbound.resolve_upstream_proxy("https", explicit="http://127.0.0.1:7890")
    assert proxy is not None

    connection = outbound._connect_via_http_tunnel(proxy, "cn.bing.com", 443, 10.0)

    request = b"".join(sent).decode("latin-1")
    assert request.startswith("CONNECT cn.bing.com:443 HTTP/1.1")
    assert fake.timeout is None  # 隧道就绪后必须回到阻塞模式
    assert connection is fake


def test_http_tunnel_rejects_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSocket:
        def sendall(self, _data: bytes) -> None:
            pass

        def recv(self, _n: int) -> bytes:
            return b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"

        def settimeout(self, _v) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(outbound.socket, "create_connection", lambda *_a, **_k: FakeSocket())
    proxy = outbound.resolve_upstream_proxy("https", explicit="http://127.0.0.1:7890")
    assert proxy is not None
    with pytest.raises(OSError, match="拒绝 CONNECT"):
        outbound._connect_via_http_tunnel(proxy, "example.com", 443, 10.0)


def test_loopback_proxy_blocked_when_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """allow_loopback_proxy=False 时，loopback 代理地址必须被拒绝。"""
    _clear_proxy_env(monkeypatch)
    # 公网性校验需要解析目标域；用固定公网地址替代真实 DNS，保证离线环境可测。
    monkeypatch.setattr(
        outbound.socket,
        "getaddrinfo",
        lambda host, port, **_k: [(outbound.socket.AF_INET, outbound.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    with pytest.raises(ValueError, match="loopback"):
        outbound.fetch_public_http(
            "https://example.com/",
            timeout=5.0,
            max_bytes=100,
            allow_loopback_proxy=False,
        )
