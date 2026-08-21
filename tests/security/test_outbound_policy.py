from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.server
import json
import socket
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.browser.security import (
    BrowserNetworkDenied,
    BrowserNetworkPolicy,
    ProxyAttributionEnvelope,
)
from crew.browser.types import BrowserConfig
from crew.core.errors import ToolError
from crew.security import provider_proxy as provider_proxy_module
from crew.security.actions import normalize_network_action
from crew.security.file_policy import FilePolicyResult
from crew.security.outbound import (
    OutboundBudgetRegistry,
    OutboundContext,
    OutboundDenied,
    OutboundGrantRegistry,
    OutboundHttpClient,
    OutboundPolicy,
    is_safe_authorization_path,
)
from crew.security.provider_proxy import (
    ProviderProxyUnavailable,
    provider_policy_proxy,
)
from crew.sites import blueprint
from crew.tools import security_guard, web_tools
from crew.tools.security_guard import (
    NetworkAuthorization,
    authorize_network_origin,
    authorize_network_url,
    fetch_public_url,
    validate_public_url,
)
from crew.wiki import parser as wiki_parser
from crew.wiki import sources as wiki_sources


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "EXAMPLE.COM.",
        "127.1",
        "2130706433",
        "0x7f000001",
        "[::ffff:127.0.0.1]",
        "bü.com",
    ],
)
def test_network_action_and_outbound_canonicalize_same_host(host: str) -> None:
    """PERM-007: every surface must agree on the exact canonical host."""
    from crew.security.outbound import canonicalize_host

    action = normalize_network_action(host, 80, "http")
    assert action.host == canonicalize_host(host)


@pytest.mark.parametrize("host", ["[fe80::1%25eth0]", "exa_mple.com", "-bad.com"])
def test_network_action_rejects_hosts_outbound_rejects(host: str) -> None:
    with pytest.raises(ValueError):
        normalize_network_action(host, 80, "http")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("http://127.1/admin", "non_public_target"),
        ("http://0177.0.0.1/admin", "non_public_target"),
        ("http://0x7f000001/admin", "non_public_target"),
        ("http://2130706433/admin", "non_public_target"),
        ("http://[::ffff:127.0.0.1]/admin", "non_public_target"),
        ("http://169.254.169.254/latest/meta-data", "metadata_target"),
        ("http://168.63.129.16/machine/?comp=goalstate", "metadata_target"),
        ("http://metadata.google.internal/computeMetadata/v1/", "metadata_target"),
        ("https://user:secret@example.com/", "userinfo_forbidden"),
        ("https://example.com:/", "invalid_port"),
        ("gopher://example.com/", "scheme_forbidden"),
        ("file:///etc/passwd", "scheme_forbidden"),
        ("data:text/html,hello", "scheme_forbidden"),
        ("blob:https://example.com/id", "scheme_forbidden"),
        ("javascript:alert(1)", "scheme_forbidden"),
        ("ws://example.com/socket", "scheme_forbidden"),
        ("ftp://example.com/archive", "scheme_forbidden"),
        ("http://example.com:0/", "invalid_port"),
        ("http://example.com:65536/", "invalid_port"),
        ("http://example.com:999999999999/", "invalid_port"),
        ("https://example.com\r\n.evil.test/", "control_character"),
        ("https://[fe80::1%25eth0]/", "zone_id_forbidden"),
    ],
)
def test_outbound_policy_rejects_ambiguous_and_sensitive_targets(
    raw: str,
    code: str,
) -> None:
    policy = OutboundPolicy()

    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url(raw)

    assert denied.value.code == code
    assert "secret" not in str(denied.value)


def test_connection_plan_cannot_be_retargeted_after_authorization() -> None:
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]
    )
    plan = policy.plan_url("https://example.com/resource")

    with pytest.raises(FrozenInstanceError):
        plan.target = policy.canonicalize_url("https://attacker.example/")[1]
    with pytest.raises(FrozenInstanceError):
        plan.endpoints = ()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://Example.COM/resource", "https://example.com/resource"),
        ("https://example.com./resource", "https://example.com/resource"),
        ("http://example.com:80/resource", "http://example.com/resource"),
        ("https://EXAMPLE.COM:443/resource", "https://example.com/resource"),
        ("https://bücher.example/resource", "https://xn--bcher-kva.example/resource"),
        ("http://127.1/resource", "http://127.0.0.1/resource"),
        ("http://0x7f000001/resource", "http://127.0.0.1/resource"),
        ("http://0177.0.0.1/resource", "http://127.0.0.1/resource"),
        ("http://2130706433/resource", "http://127.0.0.1/resource"),
    ],
)
def test_canonicalize_url_collapses_authority_spellings(first: str, second: str) -> None:
    policy = OutboundPolicy()
    first_parsed, first_target = policy.canonicalize_url(first)
    second_parsed, second_target = policy.canonicalize_url(second)
    assert first_target == second_target
    assert first_parsed.scheme.lower() == second_parsed.scheme.lower()


@pytest.mark.parametrize(
    "path",
    [
        "/openai/openai",
        "/openai/openai/issues/123",
        "/openai/openai/a..b",
        "/openai/openai/%20space",
        "/openai/openai/%E2%9C%93",
        "/openai/openai/contents/%2Egitignore",
        "/openai/openai/contents/.%2Egithub",
        "/openai/openai/%2e%2efoo",
        "/openai/openai/%2e%2e%2e",
    ],
)
def test_authorization_path_accepts_unambiguous_literal_segments(path: str) -> None:
    assert is_safe_authorization_path(path)
    _parsed, target = OutboundPolicy().canonicalize_url(
        f"https://example.com{path}"
    )
    assert target.path == path


@pytest.mark.parametrize(
    "path",
    [
        "/openai/openai/../codex",
        "/openai/openai/./issues",
        "/openai/openai\\..\\codex",
        "/openai/openai/%2e%2e/codex",
        "/openai/openai/%2E%2E/codex",
        "/openai/openai/%2f..%2fcodex",
        "/openai/openai/%5c..%5ccodex",
        "/openai/openai/%252e%252e/codex",
        "/openai/openai/%",
        "/openai/openai/%2",
        "/openai/openai/%zz",
    ],
)
def test_authorization_path_rejects_ambiguous_spellings(path: str) -> None:
    assert not is_safe_authorization_path(path)


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/../codex",
        "https://example.com/%2e%2e/codex",
        "https://example.com/%2f..%2fcodex",
        "https://example.com/%5c..%5ccodex",
        "https://example.com/%252e%252e/codex",
    ],
)
def test_browser_and_outbound_share_ambiguous_path_denial(raw: str) -> None:
    with pytest.raises(OutboundDenied) as outbound_denied:
        OutboundPolicy().canonicalize_url(raw)
    assert outbound_denied.value.code == "ambiguous_path"

    browser = BrowserNetworkPolicy(
        BrowserConfig(),
        owner="perm-007-matrix",
        allowed_origins={("https", "example.com", 443)},
    )
    with pytest.raises(BrowserNetworkDenied) as browser_denied:
        browser.validate_navigation_url(raw)
    assert browser_denied.value.code == "ambiguous_path"


@pytest.mark.asyncio
async def test_mcp_plan_url_shares_ambiguous_path_denial() -> None:
    policy = BrowserNetworkPolicy(
        BrowserConfig(),
        owner="mcp:perm-007-matrix",
        allowed_origins={("https", "example.com", 443)},
    )

    with pytest.raises(BrowserNetworkDenied) as denied:
        await policy.plan_url(
            "https://example.com/%2e%2e/codex",
            method="GET",
        )
    assert denied.value.code == "ambiguous_path"


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/a?next=%2f%2e%2e%2fadmin",
        "https://example.com/a?user=alice@example.com",
        "https://example.com/a?token=%252e%252e",
    ],
)
def test_browser_and_outbound_treat_query_forms_identically(raw: str) -> None:
    _parsed, target = OutboundPolicy().canonicalize_url(raw)
    browser = BrowserNetworkPolicy(
        BrowserConfig(),
        owner="perm-007-matrix",
        allowed_origins={("https", "example.com", 443)},
    )
    assert browser.validate_navigation_url(raw) == target.canonical_url


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:secret@example.com/a",
        "https://example.com/a?token=%zz",
    ],
)
def test_browser_and_outbound_share_userinfo_and_invalid_query_denial(
    raw: str,
) -> None:
    with pytest.raises(OutboundDenied) as outbound_denied:
        OutboundPolicy().canonicalize_url(raw)
    browser = BrowserNetworkPolicy(
        BrowserConfig(),
        owner="perm-007-matrix",
        allowed_origins={("https", "example.com", 443)},
    )
    with pytest.raises(BrowserNetworkDenied) as browser_denied:
        browser.validate_navigation_url(raw)
    assert browser_denied.value.code == outbound_denied.value.code


def test_provider_credentials_refuse_cleartext_http_origins() -> None:
    with pytest.raises(ProviderProxyUnavailable, match="invalid"):
        provider_policy_proxy("http://api.example.test/v1")


def test_proxy_attribution_envelope_binds_full_context_and_expires() -> None:
    context = OutboundContext(
        owner="owner-a",
        session="session-a",
        task="task-a",
        request="request-a",
        source="test",
        environment="environment-a",
    )
    key = b"k" * 32
    token = ProxyAttributionEnvelope.issue(
        key,
        context,
        now=10.0,
        ttl_seconds=5.0,
    )

    ProxyAttributionEnvelope.verify(token, key, context, now=14.9)

    with pytest.raises(OutboundDenied) as wrong_context:
        ProxyAttributionEnvelope.verify(
            token,
            key,
            OutboundContext(
                "owner-a",
                "session-b",
                "task-a",
                "request-a",
                "test",
                "environment-a",
            ),
            now=14.9,
        )
    assert wrong_context.value.code == "proxy_context_mismatch"

    with pytest.raises(OutboundDenied) as expired:
        ProxyAttributionEnvelope.verify(token, key, context, now=15.0)
    assert expired.value.code == "proxy_credential_expired"


def test_proxy_attribution_envelope_rejects_oversized_context() -> None:
    context = OutboundContext(
        owner="owner-a",
        session="s" * 4096,
        task="task-a",
        request="request-a",
        source="test",
        environment="environment-a",
    )

    with pytest.raises(OutboundDenied) as denied:
        ProxyAttributionEnvelope.issue(b"k" * 32, context, now=10.0)
    assert denied.value.code == "proxy_credential_too_large"


def test_proxy_attribution_envelope_rejects_wrong_magic() -> None:
    context = OutboundContext(
        "owner-a",
        "session-a",
        "task-a",
        "request-a",
        "test",
        "environment-a",
    )
    key = b"k" * 32
    token = ProxyAttributionEnvelope.issue(key, context, now=10.0)
    encoded_payload, _encoded_signature = token.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
    )
    payload["m"] = "WRONG"
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    raw_signature = hmac.new(key, raw_payload, hashlib.sha256).digest()
    bad_token = ".".join(
        base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        for value in (raw_payload, raw_signature)
    )

    with pytest.raises(OutboundDenied) as denied:
        ProxyAttributionEnvelope.verify(bad_token, key, context, now=10.0)
    assert denied.value.code == "proxy_auth_invalid"


def test_provider_proxy_cache_is_bound_to_context_and_requires_context() -> None:
    first = OutboundContext(
        "owner-a",
        "session-a",
        "task-a",
        "request-a",
        "provider_proxy",
        "environment-a",
    )
    second = OutboundContext(
        "owner-a",
        "session-b",
        "task-b",
        "request-b",
        "provider_proxy",
        "environment-a",
    )
    try:
        first_proxy = provider_policy_proxy(
            "https://api.example.test/v1",
            context=first,
        )
        second_proxy = provider_policy_proxy(
            "https://api.example.test/v1",
            context=second,
        )
        assert first_proxy.endpoint_url != second_proxy.endpoint_url
        assert first_proxy.password != second_proxy.password
        unbound_proxy = provider_policy_proxy("https://api.example.test/v1")
        assert unbound_proxy.endpoint_url not in {
            first_proxy.endpoint_url,
            second_proxy.endpoint_url,
        }
    finally:
        provider_proxy_module._close_provider_proxies()


def test_provider_proxy_wire_accepts_only_the_bound_context() -> None:
    import httpx

    context = OutboundContext(
        "owner-a",
        "session-a",
        "task-a",
        "request-a",
        "provider_proxy",
        "environment-a",
    )

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args) -> None:
            return

    with _http_server(Ok) as port:
        endpoint = f"http://127.0.0.1:{port}/v1"
        try:
            config = provider_policy_proxy(
                endpoint,
                allow_private=True,
                context=context,
            )
            with httpx.Client(
                proxy=config.httpx_proxy(),
                trust_env=False,
                timeout=2,
            ) as client:
                response = client.get(endpoint)
            assert response.status_code == 200
            assert response.text == "ok"
        finally:
            provider_proxy_module._close_provider_proxies()


@pytest.mark.asyncio
async def test_provider_proxy_dynamic_transport_binds_runtime_context() -> None:
    import httpx

    reached = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
    context = OutboundContext(
        "owner-dynamic",
        "session-dynamic",
        "task-dynamic",
        "request-dynamic",
        "provider_proxy",
        "environment-dynamic",
    )
    from crew.core import runctx

    context_values = (
        (runctx.current_owner_account_id, context.owner),
        (runctx.current_session_id, context.session),
        (runctx.current_task_runtime_id, context.task),
        (runctx.current_request_id, context.request),
        (runctx.current_workspace_id, context.environment),
    )
    config = provider_policy_proxy(endpoint, allow_private=True)
    tokens = [variable.set(value) for variable, value in context_values]
    try:
        async with httpx.AsyncClient(
            transport=config.httpx_transport(),
            trust_env=False,
            timeout=2,
        ) as client:
            response = await client.get(endpoint)
        assert response.status_code == 200
        assert response.text == "ok"
        assert reached.is_set()
    finally:
        for variable, token in reversed(list(zip((item[0] for item in context_values), tokens))):
            variable.reset(token)
        provider_proxy_module._close_provider_proxies()
        server.close()
        await server.wait_closed()


def test_dns_all_answers_must_be_acceptable() -> None:
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", 443)),
    ]
    policy = OutboundPolicy(resolver=lambda *_args, **_kwargs: answers)

    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url("https://mixed.example.test/")

    assert denied.value.code == "non_public_target"


def test_localhost_name_cannot_be_rebound_to_a_public_address() -> None:
    resolver_calls = 0

    def resolver(*_args, **_kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 80),
            )
        ]

    policy = OutboundPolicy(resolver=resolver)
    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url("http://localhost/")

    assert denied.value.code == "non_public_target"
    assert resolver_calls == 0


def test_explicit_localhost_grant_only_accepts_loopback_answers() -> None:
    context = OutboundContext(
        owner="owner",
        session="session",
        task="task",
        request="request",
        source="test",
    )
    grants = OutboundGrantRegistry()
    policy = OutboundPolicy(
        grants=grants,
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 80),
            )
        ],
    )
    token = grants.issue_private(
        context,
        host="localhost",
        port=80,
        scheme="http",
    )

    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url(
            "http://localhost/",
            context=context,
            private_grant=token,
        )

    assert denied.value.code == "localhost_address_mismatch"


def test_connection_plan_repr_never_contains_url_or_grant_secrets() -> None:
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]
    )
    plan = policy.plan_url("https://example.test/path?token=supersecret")

    rendered = repr(plan)
    assert "supersecret" not in rendered
    assert "/path" not in rendered


def test_outbound_denial_audit_never_logs_raw_url_or_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    policy = OutboundPolicy()

    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url(
            "https://alice:must-not-leak@example.test/private?token=secret"
        )

    assert denied.value.code == "userinfo_forbidden"
    assert "reason=userinfo_forbidden" in caplog.text
    assert "target=unparsed" in caplog.text
    assert "must-not-leak" not in caplog.text
    assert "token=secret" not in caplog.text
    assert "/private" not in caplog.text


def test_outbound_denial_audit_redacts_untrusted_source_labels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
    )
    context = OutboundContext(
        "owner",
        "session",
        "task",
        "request",
        "https://source.test/?token=must-not-leak",
    )

    with pytest.raises(OutboundDenied):
        policy.plan_url("https://example.test/", context=context)

    assert "source=redacted" in caplog.text
    assert "must-not-leak" not in caplog.text


def test_network_authorization_is_bound_to_the_approved_method() -> None:
    resolver_calls = 0

    def resolver(*_args, **_kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return []

    authorization = NetworkAuthorization(
        scheme="https",
        host="example.test",
        port=443,
        method="GET",
        _policy=OutboundPolicy(resolver=resolver),
    )

    with pytest.raises(ToolError, match="authorization_mismatch"):
        authorization.plan("https://example.test/resource", method="POST")

    assert resolver_calls == 0


class _ConnectedSocket:
    def __init__(self, family: int, _sock_type: int) -> None:
        self.family = family
        self.connected: tuple | None = None
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect(self, sockaddr: tuple) -> None:
        self.connected = sockaddr

    def getpeername(self) -> tuple:
        assert self.connected is not None
        return self.connected

    def close(self) -> None:
        self.closed = True


def test_dns_is_resolved_once_and_the_numeric_answer_is_pinned_for_connect() -> None:
    calls = 0
    sockets: list[_ConnectedSocket] = []

    def resolver(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "127.0.0.1"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
        ]

    def socket_factory(family: int, sock_type: int) -> _ConnectedSocket:
        sock = _ConnectedSocket(family, sock_type)
        sockets.append(sock)
        return sock

    policy = OutboundPolicy(resolver=resolver, socket_factory=socket_factory)
    plan = policy.plan_url("https://pinned.example.test/")
    connected = policy.connect_socket(plan, timeout=1.5)

    assert calls == 1
    assert connected is sockets[0]
    assert sockets[0].connected == ("93.184.216.34", 443)
    assert sockets[0].timeout == pytest.approx(1.5, abs=0.01)


def test_connect_timeout_is_one_budget_shared_across_all_pinned_addresses() -> None:
    now = [10.0]
    timeouts: list[float] = []
    sockets: list[object] = []

    class SlowFailure:
        def __init__(self, _family: int, _sock_type: int) -> None:
            sockets.append(self)

        def settimeout(self, value: float) -> None:
            timeouts.append(value)

        def connect(self, _sockaddr: tuple) -> None:
            now[0] += 0.6
            raise TimeoutError

        def close(self) -> None:
            return None

    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
            for address in ("93.184.216.34", "93.184.216.35", "93.184.216.36")
        ],
        socket_factory=SlowFailure,
        clock=lambda: now[0],
    )
    plan = policy.plan_url("https://pinned.example.test/")

    with pytest.raises(OutboundDenied) as denied:
        policy.connect_socket(plan, timeout=1.0)

    assert denied.value.code == "connect_timeout"
    assert len(sockets) == 2
    assert timeouts == pytest.approx([1.0, 0.4])


def test_private_access_requires_an_exact_single_use_context_bound_grant() -> None:
    now = [10.0]
    context = OutboundContext(
        owner="owner-a",
        session="session-a",
        task="task-a",
        request="request-a",
        source="test",
    )
    grants = OutboundGrantRegistry(clock=lambda: now[0])
    token = grants.issue_private(
        context,
        host="internal.example.test",
        port=443,
        scheme="https",
        ttl_seconds=5,
    )
    sockets: list[_ConnectedSocket] = []
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.20.30.40", 443),
            )
        ],
        socket_factory=lambda family, sock_type: sockets.append(
            _ConnectedSocket(family, sock_type)
        )
        or sockets[-1],
        grants=grants,
        clock=lambda: now[0],
    )

    with pytest.raises(OutboundDenied) as missing:
        policy.plan_url("https://internal.example.test/", context=context)
    assert missing.value.code == "private_grant_required"

    with pytest.raises(OutboundDenied) as wrong_method:
        policy.plan_url(
            "https://internal.example.test/",
            method="POST",
            context=context,
            private_grant=token,
        )
    assert wrong_method.value.code == "private_grant_invalid"

    first = policy.plan_url(
        "https://internal.example.test/",
        context=context,
        private_grant=token,
    )
    replay = policy.plan_url(
        "https://internal.example.test/",
        context=context,
        private_grant=token,
    )
    assert token not in repr(first)
    policy.connect_socket(first, context=context)
    assert len(sockets) == 1

    with pytest.raises(OutboundDenied) as reused:
        policy.connect_socket(replay, context=context)
    assert reused.value.code == "private_grant_invalid"
    assert len(sockets) == 1


def test_private_grant_cannot_be_consumed_twice_under_concurrency() -> None:
    context = OutboundContext(
        owner="owner-a",
        session="session-a",
        task="task-a",
        request="request-a",
        source="test",
    )
    grants = OutboundGrantRegistry(clock=lambda: 10.0)
    token = grants.issue_private(
        context,
        host="internal.example.test",
        port=443,
        scheme="https",
    )
    sockets: list[_ConnectedSocket] = []
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.20.30.40", 443),
            )
        ],
        socket_factory=lambda family, sock_type: sockets.append(
            _ConnectedSocket(family, sock_type)
        )
        or sockets[-1],
        grants=grants,
        clock=lambda: 10.0,
    )
    plans = [
        policy.plan_url(
            "https://internal.example.test/",
            context=context,
            private_grant=token,
        )
        for _ in range(2)
    ]
    grants._clock = lambda: time.sleep(0.05) or 10.0
    start = threading.Barrier(3)
    outcomes: list[str] = []

    def connect(plan) -> None:
        start.wait()
        try:
            policy.connect_socket(plan, context=context)
            outcomes.append("connected")
        except OutboundDenied as exc:
            outcomes.append(exc.code)

    threads = [
        threading.Thread(target=connect, args=(plan,))
        for plan in plans
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(outcomes) == ["connected", "private_grant_invalid"]
    assert len(sockets) == 1


def test_private_grant_registry_prunes_expired_unused_tokens() -> None:
    now = [10.0]
    context = OutboundContext("owner", "session", "task", "request", "test")
    grants = OutboundGrantRegistry(clock=lambda: now[0])
    expired = grants.issue_private(
        context,
        host="internal.example.test",
        port=443,
        scheme="https",
        ttl_seconds=1,
    )

    now[0] = 12.0
    active = grants.issue_private(
        context,
        host="internal.example.test",
        port=443,
        scheme="https",
    )

    assert expired not in grants._grants
    assert set(grants._grants) == {active}


def test_private_grant_rejects_cross_context_and_expiry_before_socket_creation() -> None:
    now = [20.0]
    original = OutboundContext("owner", "session", "task", "request", "test")
    other = OutboundContext("owner", "other-session", "task", "request", "test")
    grants = OutboundGrantRegistry(clock=lambda: now[0])
    token = grants.issue_private(
        original,
        host="10.0.0.9",
        port=8443,
        scheme="https",
        ttl_seconds=1,
    )
    socket_calls = 0

    def socket_factory(_family: int, _sock_type: int):
        nonlocal socket_calls
        socket_calls += 1
        raise AssertionError("denied plans must not create sockets")

    policy = OutboundPolicy(
        grants=grants,
        clock=lambda: now[0],
        socket_factory=socket_factory,
    )

    with pytest.raises(OutboundDenied) as mismatch:
        policy.plan_url(
            "https://10.0.0.9:8443/",
            context=other,
            private_grant=token,
        )
    assert mismatch.value.code == "private_grant_invalid"

    plan = policy.plan_url(
        "https://10.0.0.9:8443/",
        context=original,
        private_grant=token,
    )
    now[0] = 22.0
    with pytest.raises(OutboundDenied) as expired:
        policy.connect_socket(plan, context=original)
    assert expired.value.code == "private_grant_invalid"
    assert socket_calls == 0


def test_localhost_name_must_resolve_only_to_loopback_even_with_private_grant() -> None:
    context = OutboundContext("owner", "session", "task", "request", "test")
    grants = OutboundGrantRegistry()
    token = grants.issue_private(
        context,
        host="localhost",
        port=8080,
        scheme="http",
    )
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 8080),
            )
        ],
        grants=grants,
    )

    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url(
            "http://localhost:8080/",
            context=context,
            private_grant=token,
        )

    assert denied.value.code == "localhost_address_mismatch"


@contextmanager
def _http_server(handler):
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _private_http_client(port: int):
    context = OutboundContext("owner", "session", "task", "request", "test")
    grants = OutboundGrantRegistry()
    token = grants.issue_private(
        context,
        host="public.example.test",
        port=port,
        scheme="http",
    )
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", port),
            )
        ],
        grants=grants,
    )
    return OutboundHttpClient(policy), context, token


def test_http_redirect_is_reauthorized_and_public_to_metadata_is_blocked() -> None:
    hits: list[str] = []

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    with _http_server(Redirect) as port:
        client, context, token = _private_http_client(port)
        with pytest.raises(OutboundDenied) as denied:
            client.fetch(
                f"http://public.example.test:{port}/start",
                context=context,
                private_grant=token,
                max_redirects=3,
            )

    assert denied.value.code == "metadata_target"
    assert hits == ["/start"]


def test_http_redirects_are_denied_by_default() -> None:
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "https://example.com/next")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    with _http_server(Redirect) as port:
        client, context, token = _private_http_client(port)
        with pytest.raises(OutboundDenied) as denied:
            client.fetch(
                f"http://public.example.test:{port}/start",
                context=context,
                private_grant=token,
            )

    assert denied.value.code == "redirect_forbidden"


def test_http_client_ignores_proxy_environment_and_rewrites_host(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["host"] = self.headers.get("Host", "")
            seen["proxy_auth"] = self.headers.get("Proxy-Authorization", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *_args) -> None:
            return

    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "*")
    with _http_server(Ok) as port:
        client, context, token = _private_http_client(port)
        response = client.fetch(
            f"http://public.example.test:{port}/",
            context=context,
            private_grant=token,
        )

    assert response.body == b"OK"
    assert seen == {
        "host": f"public.example.test:{port}",
        "proxy_auth": "",
    }


def test_http_response_uses_one_total_lifetime_budget() -> None:
    class Drip(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            for value in b"12345":
                time.sleep(0.06)
                try:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        def log_message(self, *_args) -> None:
            return

    with _http_server(Drip) as port:
        client, context, token = _private_http_client(port)
        with pytest.raises(OutboundDenied) as denied:
            client.fetch(
                f"http://public.example.test:{port}/",
                context=context,
                private_grant=token,
                timeout=0.1,
            )

    assert denied.value.code == "response_timeout"


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/admin",
        "https://user:supersecret@example.com/",
        "https://example.com\r\n.evil.test/",
    ],
)
def test_web_fetch_adapter_returns_stable_redacted_outbound_denials(url: str) -> None:
    for operation in (
        lambda: validate_public_url(url),
        lambda: fetch_public_url(url),
    ):
        with pytest.raises(ToolError) as denied:
            operation()
        message = str(denied.value)
        assert "SECURITY_OUTBOUND_DENIED" in message
        assert "supersecret" not in message


def test_public_fetch_adapter_fails_closed_without_an_authorized_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        security_guard._OUTBOUND_HTTP,
        "fetch",
        lambda *_args, **_kwargs: pytest.fail("unapproved adapter must not connect"),
    )
    monkeypatch.setattr(
        security_guard._OUTBOUND_POLICY,
        "plan_url",
        lambda *_args, **_kwargs: pytest.fail("unapproved adapter must not resolve DNS"),
    )

    for operation in (
        lambda: fetch_public_url("https://example.test/"),
        lambda: validate_public_url("https://example.test/"),
    ):
        with pytest.raises(ToolError, match="authorization_required"):
            operation()


async def test_network_approval_precedes_dns_and_returns_the_connect_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def resolver(*_args, **_kwargs):
        events.append("dns")
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

    class Service:
        def authorize_network_action(self, _context, action, *, tool_name):
            assert events == []
            assert (action.host, action.port, action.protocol) == (
                "example.test",
                443,
                "https",
            )
            assert tool_name == "web_extract"
            events.append("authorize")
            return FilePolicyResult.ALLOW, "allow", None

    monkeypatch.setattr(
        security_guard,
        "_OUTBOUND_POLICY",
        OutboundPolicy(resolver=resolver),
    )
    monkeypatch.setattr(
        security_guard,
        "build_security_context",
        lambda _workspace: object(),
    )

    plan = await authorize_network_url(
        "https://example.test/path",
        method="POST",
        tool_name="web_extract",
        workspace_store=object(),
        security_service=Service(),
    )

    assert plan.target.method == "POST"
    assert plan.target.canonical_url == "https://example.test/path"
    assert events == ["authorize", "dns"]


async def test_origin_authorization_reuses_one_decision_but_replans_each_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def resolver(*_args, **_kwargs):
        events.append("dns")
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

    class Service:
        def authorize_network_action(self, *_args, **_kwargs):
            events.append("authorize")
            return FilePolicyResult.ALLOW, "allow", None

    monkeypatch.setattr(
        security_guard,
        "_OUTBOUND_POLICY",
        OutboundPolicy(resolver=resolver),
    )
    monkeypatch.setattr(
        security_guard,
        "build_security_context",
        lambda _workspace: object(),
    )

    authorization = await authorize_network_origin(
        "https://example.test/first",
        tool_name="wiki_fetch_url",
        workspace_store=object(),
        security_service=Service(),
    )
    first = authorization.plan("https://example.test/first", method="GET")
    retry = authorization.plan("https://example.test/retry", method="GET")

    assert first is not retry
    assert events == ["authorize", "dns", "dns"]
    with pytest.raises(ToolError, match="authorization_mismatch"):
        authorization.plan("https://other.example/path", method="GET")


async def test_network_context_failure_does_not_echo_host_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        def authorize_network_action(self, *_args, **_kwargs):
            raise ValueError(r"C:\private\upstream.pem TOKEN=must-not-leak")

    monkeypatch.setattr(
        security_guard,
        "build_security_context",
        lambda _workspace: object(),
    )

    with pytest.raises(ToolError) as denied:
        await authorize_network_origin(
            "https://example.test/path",
            tool_name="web_extract",
            workspace_store=object(),
            security_service=Service(),
        )
    assert str(denied.value) == "安全网络上下文无效"


async def test_web_extract_consumes_the_plan_returned_by_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 合并取舍：web_extract 走本分支的 authorize_network_tool + allowed-targets
    # 契约（plan 消费契约保留在 wiki/blueprint 路径），这里验证同一意图：
    # 授权发生在抓取前，且授权过的 authority 集合传给 fetch。
    authorized: list[tuple[str, str]] = []

    async def authorize(url, *, tool_name, workspace_store, security_service):
        del workspace_store, security_service
        authorized.append((url, tool_name))

    def fetch(url, timeout=10.0, allowed_targets=None):
        assert url == "https://example.test/article"
        assert timeout == 10.0
        assert allowed_targets is not None
        return "https://example.test/article", "<title>Safe</title><p>Body</p>"

    monkeypatch.setattr(web_tools, "authorize_network_tool", authorize)
    monkeypatch.setattr(web_tools, "_fetch_url", fetch)

    result = await web_tools.handle_web_extract(
        {"url": "https://example.test/article"},
        workspace_store=object(),
        security_service=object(),
    )

    assert '"title": "Safe"' in result
    assert authorized == [("https://example.test/article", "web_extract")]


async def test_blueprint_http_consumes_each_hop_authorization_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorized_plan = object()
    consumed: list[object] = []

    async def authorize(*_args, **kwargs):
        assert kwargs["method"] == "POST"
        return authorized_plan

    def fetch(plan, **kwargs):
        consumed.append(plan)
        assert kwargs["body"] == b'{"ok": true}'
        return SimpleNamespace(
            status=200,
            headers={},
            body=b'{"result": "safe"}',
        )

    manager = blueprint.BlueprintManager(
        object(),
        workspace_store=object(),
        security_service=object(),
    )

    monkeypatch.setattr(blueprint, "authorize_network_url", authorize)
    monkeypatch.setattr(blueprint, "fetch_authorized_url", fetch, raising=False)

    value, _summary = await manager._fetch_json(
        {"url": "https://example.test/api", "method": "POST"},
        {"ok": True},
    )

    assert value == {"result": "safe"}
    assert consumed == [authorized_plan]


async def test_blueprint_http_fails_closed_without_network_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = blueprint.BlueprintManager(object())

    monkeypatch.setattr(
        blueprint,
        "fetch_authorized_url",
        lambda *_args, **_kwargs: pytest.fail("missing authorization must not connect"),
    )

    with pytest.raises(ToolError, match="authorization_unavailable"):
        await manager._fetch_json(
            {"url": "https://example.test/api", "method": "GET"},
            None,
        )


def test_wiki_fetch_replans_from_the_approved_origin_for_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = [object(), object()]
    issued: list[tuple[str, str]] = []
    consumed: list[object] = []

    class Authorization:
        def plan(self, url: str, *, method: str):
            issued.append((url, method))
            return plans[len(issued) - 1]

    def fetch(plan, **_kwargs):
        consumed.append(plan)
        return SimpleNamespace(
            final_url="https://example.test/final",
            body=b"<html><body><p>Safe wiki body</p></body></html>",
            content_type="text/plain; charset=utf-8",
            charset="utf-8",
        )

    monkeypatch.setattr(security_guard, "fetch_authorized_url", fetch)

    for _attempt in range(2):
        markdown, final_url = wiki_parser.fetch_url_to_markdown(
            "https://example.test/article",
            authorization=Authorization(),
        )
        assert "Safe wiki body" in markdown
        assert final_url == "https://example.test/final"

    assert issued == [
        ("https://example.test/article", "GET"),
        ("https://example.test/article", "GET"),
    ]
    assert consumed == plans


def test_youtube_adapter_cannot_bypass_approved_origin_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued: list[tuple[str, str]] = []
    plan = object()

    class Authorization:
        origin = ("https", "www.youtube.com", 443)

        def plan(self, url: str, *, method: str):
            issued.append((url, method))
            return plan

    class FakeTranscriptApi:
        def __init__(self, *, http_client) -> None:
            self.http_client = http_client

        def fetch(self, video_id: str):
            response = self.http_client.get(
                f"https://www.youtube.com/watch?v={video_id}"
            )
            assert response.status_code == 200
            return [SimpleNamespace(text="safe transcript", start=0.0)]

    def fetch(used_plan, **_kwargs):
        assert used_plan is plan
        return SimpleNamespace(
            status=200,
            headers={"content-type": "text/plain"},
            body=b"ok",
            final_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            charset="utf-8",
        )

    monkeypatch.setitem(
        sys.modules,
        "youtube_transcript_api",
        SimpleNamespace(YouTubeTranscriptApi=FakeTranscriptApi),
    )
    monkeypatch.setattr(security_guard, "fetch_authorized_url", fetch)

    markdown, video_id = wiki_sources.fetch_youtube_transcript(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        authorizations=(Authorization(),),
    )

    assert video_id == "dQw4w9WgXcQ"
    assert "safe transcript" in markdown
    assert issued == [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "GET")
    ]


def test_prepared_http_plan_is_the_only_dns_decision_consumed_by_connect() -> None:
    calls = 0

    def resolver(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 80),
            )
        ]

    sockets: list[_ConnectedSocket] = []
    policy = OutboundPolicy(
        resolver=resolver,
        socket_factory=lambda family, sock_type: sockets.append(
            _ConnectedSocket(family, sock_type)
        )
        or sockets[-1],
    )
    client = OutboundHttpClient(policy)
    plan = policy.plan_url("http://pinned.example.test/resource", method="GET")

    with pytest.raises(OutboundDenied) as transport:
        client.fetch_plan(plan, method="GET", timeout=0.1)

    assert transport.value.code == "http_transport_failed"
    assert calls == 1
    assert sockets[0].connected == ("93.184.216.34", 80)

    with pytest.raises(OutboundDenied) as replay:
        client.fetch_plan(plan, method="GET", timeout=0.1)
    assert replay.value.code == "plan_reused"
    assert calls == 1


def test_prepared_http_plan_binds_method_and_request_limits_before_socket_creation() -> None:
    socket_calls = 0

    def socket_factory(_family: int, _sock_type: int):
        nonlocal socket_calls
        socket_calls += 1
        raise AssertionError("invalid authorized request must not create a socket")

    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ],
        socket_factory=socket_factory,
    )
    client = OutboundHttpClient(policy)

    wrong_method = policy.plan_url("https://example.test/", method="GET")
    with pytest.raises(OutboundDenied) as mismatch:
        client.fetch_plan(wrong_method, method="POST")
    assert mismatch.value.code == "authorization_mismatch"

    oversized = policy.plan_url("https://example.test/", method="POST")
    with pytest.raises(OutboundDenied) as body:
        client.fetch_plan(
            oversized,
            method="POST",
            body=b"12345",
            max_request_bytes=4,
        )
    assert body.value.code == "request_too_large"
    assert socket_calls == 0


def test_http_client_connection_limit_rejects_before_socket_creation() -> None:
    socket_calls = 0

    def socket_factory(_family: int, _sock_type: int):
        nonlocal socket_calls
        socket_calls += 1
        raise AssertionError("connection limit must reject before socket creation")

    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ],
        socket_factory=socket_factory,
    )
    client = OutboundHttpClient(policy)
    client._connection_slots = threading.BoundedSemaphore(1)
    assert client._connection_slots.acquire(blocking=False)
    try:
        with pytest.raises(OutboundDenied) as denied:
            client.fetch("https://example.test/")
    finally:
        client._connection_slots.release()

    assert denied.value.code == "connection_limit"
    assert socket_calls == 0


def test_outbound_budget_is_shared_by_owner_and_task_across_clients() -> None:
    registry = OutboundBudgetRegistry(max_connections=1, max_bytes=8)
    first = OutboundContext("owner", "session-a", "task", "request-a", "test")
    second = OutboundContext("owner", "session-b", "task", "request-b", "test")
    other_task = OutboundContext("owner", "session-c", "other-task", "request-c", "test")

    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_budget() -> None:
        lease = registry.acquire(first, byte_budget=8)
        try:
            acquired.set()
            if not release.wait(2):
                raise AssertionError("budget holder did not receive release")
        except (AssertionError, OutboundDenied) as exc:
            errors.append(exc)
        finally:
            lease.release()

    thread = threading.Thread(target=hold_budget)
    thread.start()
    assert acquired.wait(2)
    try:
        with pytest.raises(OutboundDenied) as denied:
            registry.acquire(second, byte_budget=1)
        assert denied.value.code == "aggregate_connection_limit"

        other_lease = registry.acquire(other_task, byte_budget=1)
        other_lease.release()
        other_lease.release()
    finally:
        release.set()
        thread.join(timeout=2)

    assert errors == []
    assert not thread.is_alive()
    assert registry.usage(first) == (0, 0)
    assert registry.usage(second) == (0, 0)


def test_outbound_budget_rejects_aggregate_bytes_before_socket_creation() -> None:
    registry = OutboundBudgetRegistry(max_connections=2, max_bytes=8, max_scopes=2)
    context = OutboundContext("owner", "session", "task", "request", "test")
    another_context = OutboundContext("another-owner", "session", "task", "request", "test")
    lease = registry.acquire(context, byte_budget=7)
    socket_calls = 0

    def socket_factory(_family: int, _sock_type: int):
        nonlocal socket_calls
        socket_calls += 1
        raise AssertionError("aggregate byte limit must reject before socket creation")

    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))
        ],
        socket_factory=socket_factory,
    )
    client = OutboundHttpClient(policy, budget=registry)
    plan = policy.plan_url("http://example.test/", context=context)

    try:
        with pytest.raises(OutboundDenied) as denied:
            client.fetch_plan(plan, method="GET", max_bytes=2, context=context)
        assert denied.value.code == "aggregate_byte_limit"
        assert socket_calls == 0
        assert registry.usage(context) == (1, 7)

        with pytest.raises(OutboundDenied) as fresh_denied:
            registry.acquire(another_context, byte_budget=9)
        assert fresh_denied.value.code == "aggregate_byte_limit"

        other_lease = registry.acquire(another_context, byte_budget=1)
        other_lease.release()
    finally:
        lease.release()


def test_outbound_budget_releases_after_transport_exception() -> None:
    registry = OutboundBudgetRegistry(max_connections=1, max_bytes=8)
    context = OutboundContext("owner", "session-a", "task", "request-a", "test")
    other_context = OutboundContext("owner", "session-b", "task", "request-b", "test")
    started = threading.Event()
    release = threading.Event()

    class BlockingSocket:
        def settimeout(self, _value: float) -> None:
            return None

        def connect(self, _sockaddr: tuple) -> None:
            started.set()
            if not release.wait(2):
                raise AssertionError("transport did not receive release")
            raise OSError("simulated connect failure")

        def close(self) -> None:
            return None

    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))
        ],
        socket_factory=lambda _family, _sock_type: BlockingSocket(),
    )
    first_client = OutboundHttpClient(policy, budget=registry)
    second_client = OutboundHttpClient(policy, budget=registry)
    first_plan = policy.plan_url("http://example.test/", context=context)
    second_plan = policy.plan_url("http://example.test/", context=other_context)
    outcomes: list[str] = []

    def run_first_request() -> None:
        try:
            first_client.fetch_plan(first_plan, method="GET", max_bytes=8, context=context)
        except (AssertionError, OutboundDenied) as exc:
            outcomes.append(exc.code if isinstance(exc, OutboundDenied) else str(exc))

    thread = threading.Thread(target=run_first_request)
    thread.start()
    assert started.wait(2)

    with pytest.raises(OutboundDenied) as denied:
        second_client.fetch_plan(second_plan, method="GET", max_bytes=1, context=other_context)
    assert denied.value.code == "aggregate_connection_limit"

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcomes == ["connect_failed"]
    assert registry.usage(context) == (0, 0)
    lease = registry.acquire(context, byte_budget=8)
    lease.release()


def test_prepared_http_plan_rejects_oversized_headers_before_socket_creation() -> None:
    socket_calls = 0

    def socket_factory(_family: int, _sock_type: int):
        nonlocal socket_calls
        socket_calls += 1
        raise AssertionError("oversized headers must not create a socket")

    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ],
        socket_factory=socket_factory,
    )
    client = OutboundHttpClient(policy)
    plan = policy.plan_url("https://example.test/", method="GET")

    with pytest.raises(OutboundDenied) as denied:
        client.fetch_plan(
            plan,
            method="GET",
            headers={"X-Oversized": "x" * (64 * 1024)},
        )

    assert denied.value.code == "request_headers_too_large"
    assert socket_calls == 0

    framing = policy.plan_url("https://example.test/", method="POST")
    with pytest.raises(OutboundDenied) as forbidden:
        client.fetch_plan(
            framing,
            method="POST",
            body=b"x",
            headers={"Content-Length": "0"},
        )
    assert forbidden.value.code == "forbidden_header"
    assert socket_calls == 0


def test_http_client_rejects_oversized_response_headers() -> None:
    class OversizedHeaders(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            for index in range(80):
                self.send_header(f"X-Padding-{index}", "x" * 1024)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    with _http_server(OversizedHeaders) as port:
        client, context, token = _private_http_client(port)
        with pytest.raises(OutboundDenied) as denied:
            client.fetch(
                f"http://public.example.test:{port}/headers",
                context=context,
                private_grant=token,
            )

    assert denied.value.code == "response_headers_too_large"


def test_http_client_rejects_ambiguous_response_framing() -> None:
    class AmbiguousResponse(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Content-Length", "1")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    with _http_server(AmbiguousResponse) as port:
        client, context, token = _private_http_client(port)
        with pytest.raises(OutboundDenied) as denied:
            client.fetch(
                f"http://public.example.test:{port}/framing",
                context=context,
                private_grant=token,
            )

    assert denied.value.code == "ambiguous_response_framing"


@pytest.mark.parametrize(
    ("encoding", "expected_code"),
    [
        ("gzip", "unsupported_response_encoding"),
        ("br", "unsupported_response_encoding"),
        ("identity, gzip", "unsupported_response_encoding"),
        ("", "unsupported_response_encoding"),
    ],
)
def test_http_client_rejects_unsupported_response_encoding(
    encoding: str, expected_code: str
) -> None:
    class EncodedResponse(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    with _http_server(EncodedResponse) as port:
        client, context, token = _private_http_client(port)
        with pytest.raises(OutboundDenied) as denied:
            client.fetch(
                f"http://public.example.test:{port}/encoded",
                context=context,
                private_grant=token,
            )

    assert denied.value.code == expected_code


def test_http_client_rejects_duplicate_identity_response_encoding() -> None:
    class DuplicateIdentity(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Encoding", "identity")
            self.send_header("Content-Encoding", "identity")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    with _http_server(DuplicateIdentity) as port:
        client, context, token = _private_http_client(port)
        with pytest.raises(OutboundDenied) as denied:
            client.fetch(
                f"http://public.example.test:{port}/duplicate-identity",
                context=context,
                private_grant=token,
            )

    assert denied.value.code == "unsupported_response_encoding"


def test_dns_answer_count_is_bounded_before_any_connect_attempt() -> None:
    answers = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (f"93.184.216.{index}", 443),
        )
        for index in range(1, 34)
    ]
    policy = OutboundPolicy(resolver=lambda *_args, **_kwargs: answers)

    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url("https://many-addresses.example.test/")

    assert denied.value.code == "dns_answer_limit"


def test_managed_tool_download_uses_shared_pinned_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.tools import managed_tools

    payload = b"pinned archive"
    seen: dict[str, object] = {}

    class PinnedClient:
        def fetch(self, url: str, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return SimpleNamespace(
                final_url=url,
                status=200,
                headers={},
                body=payload,
                content_type="application/octet-stream",
                charset="utf-8",
            )

    monkeypatch.setattr(
        managed_tools,
        "_MANAGED_TOOL_HTTP",
        PinnedClient(),
        raising=False,
    )
    target = tmp_path / "rg.tar.gz"

    managed_tools._download_to("https://example.test/rg.tar.gz", target)

    assert target.read_bytes() == payload
    assert seen["kwargs"] == {
        "method": "GET",
        "timeout": managed_tools._DOWNLOAD_TIMEOUT_SECONDS,
        "max_bytes": managed_tools._DOWNLOAD_MAX_BYTES,
        "max_redirects": 3,
    }


def test_internal_mcp_callback_uses_context_bound_pinned_loopback_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.gateway import mcp_server

    seen: dict[str, object] = {}

    class PinnedClient:
        def fetch_plan(self, plan, **kwargs):
            seen["plan"] = plan
            seen["kwargs"] = kwargs
            return SimpleNamespace(
                status=200,
                body=b'{"ok":true}',
                charset="utf-8",
            )

    monkeypatch.setattr(mcp_server, "_INTERNAL_HTTP", PinnedClient(), raising=False)

    result = mcp_server._post_internal(
        "http://127.0.0.1:43123",
        "must-not-leak",
        "/api/internal/interactions/ask",
        {"question": "safe"},
    )

    assert result == {"ok": True}
    plan = seen["plan"]
    assert plan.target.audit_summary == "http://127.0.0.1:43123"
    assert plan.context is not None
    assert seen["kwargs"]["headers"]["Authorization"] == "Bearer must-not-leak"
    assert seen["kwargs"]["max_bytes"] == mcp_server._MCP_MAX_RESULT_BYTES


def test_internal_mcp_callback_rejects_remote_gateway_without_leaking_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from crew.gateway import mcp_server

    with pytest.raises(RuntimeError) as denied:
        mcp_server._post_internal(
            "https://attacker.example",
            "must-not-leak",
            "/api/internal/interactions/ask",
            {},
        )

    assert "must-not-leak" not in str(denied.value)
    assert "must-not-leak" not in caplog.text
