from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.security import outbound
from crew.security.outbound import parse_public_http_target
from crew.tools import web_tools
from crew.tools.security_guard import authorize_configured_mcp_call, authorize_exec_tool


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
async def test_web_search_returns_only_real_result_links(monkeypatch) -> None:
    authorized: list[str] = []

    async def allow(url: str, **_kwargs) -> None:
        authorized.append(url)

    source = """
      <header><a href="/">Bing</a></header>
      <ol><li class="b_algo"><h2>
        <a href="https://example.com/docs">Example Docs</a>
      </h2></li></ol>
      <a href="#feedback">Feedback</a>
    """
    monkeypatch.setattr(web_tools, "authorize_network_tool", allow)
    monkeypatch.setattr(web_tools, "_fetch_url", lambda _url, *_args: (_url, source))

    payload = json.loads(await web_tools.handle_web_search({"query": "example"}))

    assert len(authorized) == 1
    assert payload["results"] == [
        {"title": "Example Docs", "url": "https://example.com/docs"}
    ]


@pytest.mark.asyncio
async def test_web_extract_authorizes_before_fetch(monkeypatch) -> None:
    order: list[str] = []

    async def allow(_url: str, **_kwargs) -> None:
        order.append("authorize")

    def fetch(url: str, *_args) -> tuple[str, str]:
        order.append("fetch")
        return url, "<html><title>Safe</title><body>Body</body></html>"

    monkeypatch.setattr(web_tools, "authorize_network_tool", allow)
    monkeypatch.setattr(web_tools, "_fetch_url", fetch)

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert order == ["authorize", "fetch"]
    assert payload["title"] == "Safe"
    assert payload["text"] == "Safe Body"


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

    assert authorized == ["https://example.com/a", redirected]
    assert payload["url"] == redirected


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
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    # 本机 fake-ip DNS 会把公网域名解析进保留段；用假公网地址隔离环境依赖。
    import socket as _socket

    monkeypatch.setattr(
        outbound,
        "_resolve_public_addresses",
        lambda target: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", target.port))
        ],
    )
    with pytest.raises(ValueError, match="loopback"):
        outbound.fetch_public_http(
            "https://example.com/",
            timeout=5.0,
            max_bytes=100,
            allow_loopback_proxy=False,
        )
