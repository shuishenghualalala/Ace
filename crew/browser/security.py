"""Network compatibility helpers and authenticated loopback proxy."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from collections.abc import Iterable
from urllib.parse import urlsplit

from crew.browser.types import BrowserConfig
from crew.security.outbound import (
    ConnectionPlan,
    OutboundContext,
    OutboundDenied,
    OutboundGrantRegistry,
    OutboundPolicy,
    canonicalize_host,
)
from crew.state.logging import get_logger

log = get_logger("browser.security")

_DANGEROUS_NAVIGATION_SCHEMES = frozenset(
    {
        "about",
        "chrome",
        "chrome-extension",
        "data",
        "devtools",
        "file",
        "filesystem",
        "javascript",
        "vbscript",
        "view-source",
    }
)
_LOCAL_NAVIGATION_SCHEMES = frozenset({"blob", "crew-artifact"})


class BrowserNetworkDenied(ValueError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        text = str(message)
        if code is None and text.startswith("SECURITY_OUTBOUND_DENIED:"):
            code = text.partition(":")[2] or "policy_denied"
        self.code = str(code or "policy_denied")
        super().__init__(text)


class ProxyAttributionEnvelope:
    """Short-lived, signed proxy credential bound to one host context."""

    _MAGIC = "ACEP"
    _VERSION = 1
    _MAX_TTL_SECONDS = 60.0
    _MAX_PAYLOAD_BYTES = 1024
    _MAX_TOKEN_BYTES = 2048
    _MAX_CONTEXT_FIELD_BYTES = 256

    @classmethod
    def issue(
        cls,
        key: bytes,
        context: OutboundContext,
        *,
        now: float | None = None,
        ttl_seconds: float = 30.0,
    ) -> str:
        context.require_complete()
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise OutboundDenied("proxy_credential_ttl_invalid") from exc
        if not math.isfinite(ttl) or not 0 < ttl <= cls._MAX_TTL_SECONDS:
            raise OutboundDenied("proxy_credential_ttl_invalid")
        fields = [
            str(context.owner),
            str(context.session),
            str(context.task),
            str(context.request),
            str(context.source),
            str(context.environment),
        ]
        if any(
            len(value.encode("utf-8")) > cls._MAX_CONTEXT_FIELD_BYTES
            for value in fields
        ):
            raise OutboundDenied("proxy_credential_too_large")
        payload = json.dumps(
            {
                "m": cls._MAGIC,
                "v": cls._VERSION,
                "exp": (now if now is not None else time.monotonic()) + ttl,
                "n": secrets.token_urlsafe(12),
                "c": fields,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > cls._MAX_PAYLOAD_BYTES:
            raise OutboundDenied("proxy_credential_too_large")
        signature = hmac.new(key, payload, hashlib.sha256).digest()
        token = ".".join(
            cls._b64(value)
            for value in (payload, signature)
        )
        if len(token.encode("ascii")) > cls._MAX_TOKEN_BYTES:
            raise OutboundDenied("proxy_credential_too_large")
        return token

    @classmethod
    def verify(
        cls,
        token: str,
        key: bytes,
        expected: OutboundContext,
        *,
        now: float | None = None,
    ) -> None:
        actual = cls.decode(token, key, now=now)
        expected.require_complete()
        if actual != expected:
            raise OutboundDenied("proxy_context_mismatch")

    @classmethod
    def decode(
        cls,
        token: str,
        key: bytes,
        *,
        now: float | None = None,
    ) -> OutboundContext:
        if (
            not isinstance(token, str)
            or len(token.encode("utf-8")) > cls._MAX_TOKEN_BYTES
        ):
            raise OutboundDenied("proxy_credential_too_large")
        try:
            encoded_payload, encoded_signature = token.split(".")
            payload = cls._unb64(encoded_payload)
            signature = cls._unb64(encoded_signature)
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise OutboundDenied("proxy_auth_invalid") from exc
        if (
            len(payload) > cls._MAX_PAYLOAD_BYTES
            or len(signature) != hashlib.sha256().digest_size
        ):
            raise OutboundDenied("proxy_auth_invalid")
        if not hmac.compare_digest(
            signature,
            hmac.new(key, payload, hashlib.sha256).digest(),
        ):
            raise OutboundDenied("proxy_auth_invalid")
        try:
            value = json.loads(payload.decode("utf-8"))
            expires = float(value["exp"])
            fields = value["c"]
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise OutboundDenied("proxy_auth_invalid") from exc
        if (
            value.get("m") != cls._MAGIC
            or value.get("v") != cls._VERSION
            or not math.isfinite(expires)
        ):
            raise OutboundDenied("proxy_auth_invalid")
        current = time.monotonic() if now is None else float(now)
        if current >= expires:
            raise OutboundDenied("proxy_credential_expired")
        if expires - current > cls._MAX_TTL_SECONDS + 1e-6:
            raise OutboundDenied("proxy_auth_invalid")
        if (
            not isinstance(fields, list)
            or len(fields) != 6
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > cls._MAX_CONTEXT_FIELD_BYTES
                for item in fields
            )
        ):
            raise OutboundDenied("proxy_auth_invalid")
        return OutboundContext(*fields)

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        if not value:
            raise ValueError("empty base64")
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _normalized_host(host: str) -> str:
    raw = str(host or "").strip()
    if not raw:
        return ""
    try:
        return ipaddress.ip_address(raw).compressed.lower()
    except ValueError:
        pass
    # URL/DNS libraries apply IDNA and accept Unicode dot variants. Apply the
    # same canonicalization before policy comparison so blocked public domains
    # cannot be bypassed with visually equivalent Unicode hostnames.
    raw = raw.translate(str.maketrans({"。": ".", "．": ".", "｡": "."})).rstrip(".")
    try:
        canonical = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise BrowserNetworkDenied("主机名 IDNA 编码无效") from exc
    if len(canonical) > 253 or any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in canonical.split(".")
    ):
        raise BrowserNetworkDenied("主机名格式无效")
    return canonical


def _runtime_proxy_context(owner: str) -> OutboundContext | None:
    """Capture host-owned attribution when a proxy is created in a task."""
    try:
        from crew.core.runctx import (
            current_request_id,
            current_session_id,
            current_task_runtime_id,
            current_workspace_id,
        )

        context = OutboundContext(
            owner=owner,
            session=current_session_id.get(),
            task=current_task_runtime_id.get(),
            request=current_request_id.get(),
            source="browser_proxy",
            environment=current_workspace_id.get(),
        )
        context.require_complete()
    except (ImportError, OutboundDenied):
        return None
    return context


@dataclass(frozen=True)
class ResolvedTarget:
    hostname: str
    ip: str
    port: int


class BrowserNetworkPolicy:
    def __init__(
        self,
        config: BrowserConfig,
        *,
        owner: str = "browser",
        allowed_origins: set[tuple[str, str, int]] | None = None,
        default_allow_public: bool = False,
        proxy_context: OutboundContext | None = None,
    ) -> None:
        self.config = config
        self._owner = str(owner or "browser")
        if proxy_context is not None:
            proxy_context.require_complete()
            if proxy_context.owner != self._owner:
                raise OutboundDenied("context_mismatch")
        self._proxy_context = proxy_context or _runtime_proxy_context(self._owner)
        self._grants = OutboundGrantRegistry()
        self.outbound = OutboundPolicy(grants=self._grants)
        self._default_allow_public = bool(default_allow_public)
        self._allowed_origins = {
            (
                str(scheme).lower(),
                canonicalize_host(host),
                int(port),
            )
            for scheme, host, port in (allowed_origins or set())
        }
        self._blocked_hosts = {canonicalize_host(item) for item in config.blocked_hosts}
        self._allowed_private_hosts = {
            canonicalize_host(item) for item in config.allowed_private_hosts
        }

    def validate_navigation_url(self, url: str) -> str:
        """Canonicalize a navigation and reject local/private bypasses early."""
        raw = str(url or "")
        if (
            not raw
            or raw != raw.strip()
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw)
        ):
            raise BrowserNetworkDenied("URL 不能为空")
        raw_scheme, separator, _remainder = raw.partition(":")
        canonical_scheme = re.sub(r"[\x00-\x20\x7f]", "", raw_scheme)
        if separator and re.fullmatch(
            r"[a-z][a-z0-9+.-]*",
            canonical_scheme,
            re.IGNORECASE,
        ):
            scheme = canonical_scheme.lower()
            if scheme == "about" and raw.lower() == "about:blank":
                return raw
            if scheme in _LOCAL_NAVIGATION_SCHEMES:
                return raw
            if scheme in _DANGEROUS_NAVIGATION_SCHEMES:
                raise BrowserNetworkDenied(f"浏览器 URL 协议不允许: {scheme}")
        try:
            _parsed, target = self.outbound.canonicalize_url(raw)
            host = self.validate_hostname(target.host)
            self._authorize_origin(target.scheme, host, target.port)
            if (
                host == "localhost" or host.endswith(".localhost")
            ) and host not in self._allowed_private_hosts:
                raise OutboundDenied("non_public_target")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                self.outbound.validate_resolved_address(
                    host,
                    allow_private=host in self._allowed_private_hosts,
                )
            return target.canonical_url
        except OutboundDenied as exc:
            raise BrowserNetworkDenied(f"SECURITY_OUTBOUND_DENIED:{exc.code}") from exc

    def validate_hostname(self, hostname: str) -> str:
        try:
            host = canonicalize_host(hostname)
        except OutboundDenied as exc:
            raise BrowserNetworkDenied(f"SECURITY_OUTBOUND_DENIED:{exc.code}") from exc
        if not host:
            raise BrowserNetworkDenied("主机名不能为空")
        if host in self._blocked_hosts or any(host.endswith(f".{item}") for item in self._blocked_hosts):
            raise BrowserNetworkDenied("该主机已被管理员策略阻止")
        return host

    def _authorize_origin(self, scheme: str, host: str, port: int) -> None:
        if self._default_allow_public:
            return
        canonical_host = canonicalize_host(host)
        if canonical_host in self._allowed_private_hosts:
            return
        if (
            str(scheme).lower(),
            canonical_host,
            int(port),
        ) not in self._allowed_origins:
            raise OutboundDenied("destination_not_authorized")

    def validate_ip(self, hostname: str, value: str) -> str:
        host = self.validate_hostname(hostname)
        try:
            return self.outbound.validate_resolved_address(
                value,
                allow_private=host in self._allowed_private_hosts,
            )
        except OutboundDenied as exc:
            raise BrowserNetworkDenied(f"SECURITY_OUTBOUND_DENIED:{exc.code}") from exc

    def _context(self, proxy_context: OutboundContext | None = None) -> OutboundContext:
        if proxy_context is not None:
            proxy_context.require_complete()
            if self._proxy_context is not None and proxy_context != self._proxy_context:
                raise OutboundDenied("context_mismatch")
            return proxy_context
        if self._proxy_context is not None:
            return self._proxy_context
        return OutboundContext(
            owner=self._owner,
            session="browser",
            task="browser-egress",
            request=secrets.token_urlsafe(18),
            source="browser_proxy",
            environment="browser",
        )

    @property
    def proxy_context(self) -> OutboundContext | None:
        return self._proxy_context

    @property
    def owner(self) -> str:
        return self._owner

    def _private_grant(
        self,
        context: OutboundContext,
        *,
        host: str,
        port: int,
        scheme: str,
        method: str,
    ) -> str:
        if host not in self._allowed_private_hosts:
            return ""
        return self._grants.issue_private(
            context,
            host=host,
            port=port,
            scheme=scheme,
            method=method,
            ttl_seconds=30,
        )

    async def plan_authority(
        self,
        hostname: str,
        port: int,
        *,
        scheme: str,
        method: str = "CONNECT",
        proxy_context: OutboundContext | None = None,
    ) -> ConnectionPlan:
        host = self.validate_hostname(hostname)
        try:
            self._authorize_origin(scheme, host, int(port))
            context = self._context(proxy_context)
            grant = self._private_grant(
                context,
                host=host,
                port=int(port),
                scheme=scheme,
                method=method,
            )
            return await asyncio.to_thread(
                self.outbound.plan_authority,
                host,
                int(port),
                scheme=scheme,
                method=method,
                context=context,
                private_grant=grant,
            )
        except OutboundDenied as exc:
            raise BrowserNetworkDenied(f"SECURITY_OUTBOUND_DENIED:{exc.code}") from exc

    async def plan_url(
        self,
        url: str,
        *,
        method: str,
        allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
        proxy_context: OutboundContext | None = None,
    ) -> ConnectionPlan:
        try:
            _parsed, target = self.outbound.canonicalize_url(
                url,
                method=method,
                allowed_schemes=allowed_schemes,
            )
            self.validate_hostname(target.host)
            self._authorize_origin(target.scheme, target.host, target.port)
            context = self._context(proxy_context)
            grant = self._private_grant(
                context,
                host=target.host,
                port=target.port,
                scheme=target.scheme,
                method=target.method,
            )
            return await asyncio.to_thread(
                self.outbound.plan_url,
                target.canonical_url,
                method=method,
                allowed_schemes=allowed_schemes,
                context=context,
                private_grant=grant,
            )
        except OutboundDenied as exc:
            raise BrowserNetworkDenied(f"SECURITY_OUTBOUND_DENIED:{exc.code}") from exc

    async def connect(
        self,
        plan: ConnectionPlan,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            sock = await asyncio.to_thread(
                self.outbound.connect_socket,
                plan,
                context=plan.context,
                timeout=10.0,
            )
        except OutboundDenied as exc:
            raise BrowserNetworkDenied(f"SECURITY_OUTBOUND_DENIED:{exc.code}") from exc
        try:
            return await asyncio.open_connection(sock=sock)
        except BaseException:
            sock.close()
            raise

    async def resolve(self, hostname: str, port: int) -> ResolvedTarget:
        candidates = await self.resolve_candidates(hostname, port)
        return candidates[0]

    async def resolve_candidates(self, hostname: str, port: int) -> list[ResolvedTarget]:
        plan = await self.plan_authority(
            hostname,
            int(port),
            scheme="https" if int(port) == 443 else "http",
        )
        return [
            ResolvedTarget(plan.target.host, endpoint.address, plan.target.port)
            for endpoint in plan.endpoints
        ]


class LoopbackPolicyProxy:
    """Authenticated mandatory HTTP proxy with DNS-pinned upstream sockets."""

    def __init__(
        self,
        policy: BrowserNetworkPolicy,
        *,
        context: OutboundContext | None = None,
    ) -> None:
        self.policy = policy
        if context is not None:
            context.require_complete()
            if policy.proxy_context is not None and policy.proxy_context != context:
                raise OutboundDenied("context_mismatch")
            if policy.proxy_context is None:
                policy._proxy_context = context
        self._proxy_context = context or policy.proxy_context
        self._server: asyncio.AbstractServer | None = None
        self._username = "crew"
        self._credential_key = secrets.token_bytes(32)
        self._password = secrets.token_urlsafe(32)
        self._connection_slots = asyncio.Semaphore(64)
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._client_writers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str:
        """Compatibility alias for the credential-free endpoint."""
        return self.endpoint_url

    @property
    def endpoint_url(self) -> str:
        """Return the credential-free endpoint for structured proxy clients."""

        if self._server is None or not self._server.sockets:
            return ""
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    @property
    def credentials(self) -> tuple[str, str]:
        """Return credentials separately so callers never serialize them in a URL."""

        return self._username, self._password

    def credentials_for(self, context: OutboundContext) -> tuple[str, str]:
        """Issue a credential for one explicit context without exposing it in a URL."""

        return self._username, ProxyAttributionEnvelope.issue(
            self._credential_key,
            context,
        )

    async def start(self) -> str:
        if self._server is None:
            if self._proxy_context is not None:
                self._password = ProxyAttributionEnvelope.issue(
                    self._credential_key,
                    self._proxy_context,
                )
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self.url

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        writers = tuple(self._client_writers)
        tasks = tuple(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for writer in writers:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        self._client_writers.clear()
        self._client_tasks.clear()

    def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._client_writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    _CONNECT_TIMEOUT = 10.0

    async def _connect(
        self,
        plan: ConnectionPlan,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.wait_for(
                self.policy.connect(plan),
                timeout=self._CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError, BrowserNetworkDenied) as exc:
            reason = (
                exc.code
                if isinstance(exc, BrowserNetworkDenied)
                else "network_unavailable"
            )
            log.info(
                "browser proxy upstream blocked target=%s reason=%s",
                plan.target.audit_summary,
                reason,
            )
            raise

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(self._connection_slots.acquire(), timeout=1.0)
        except asyncio.TimeoutError:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self._client_writers.discard(writer)
            return
        try:
            await self._handle_one(reader, writer)
        finally:
            self._connection_slots.release()
            self._client_writers.discard(writer)

    async def _handle_one(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        remote_writer: asyncio.StreamWriter | None = None
        response_started = False
        audit_target = "unparsed"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(head) > 64 * 1024:
                raise BrowserNetworkDenied("代理请求头过大")
            first, *header_lines = head.decode("latin-1").split("\r\n")
            headers = self._parse_headers(header_lines)
            authorizations = [
                value for name, value in headers if name == "proxy-authorization"
            ]
            if len(authorizations) != 1:
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                    b'Proxy-Authenticate: Basic realm="Crew Browser"\r\n'
                    b'Content-Length: 0\r\nConnection: close\r\n\r\n'
                )
                await writer.drain()
                return
            try:
                scheme, encoded = authorizations[0].split(" ", 1)
                if scheme.lower() != "basic":
                    raise ValueError("proxy auth scheme")
                raw_credentials = base64.b64decode(encoded, validate=True).decode("utf-8")
                username, password = raw_credentials.split(":", 1)
            except (ValueError, UnicodeDecodeError) as exc:
                raise BrowserNetworkDenied(
                    "SECURITY_OUTBOUND_DENIED:proxy_auth_invalid",
                    code="proxy_auth_invalid",
                ) from exc
            if username != self._username:
                raise BrowserNetworkDenied(
                    "SECURITY_OUTBOUND_DENIED:proxy_auth_invalid",
                    code="proxy_auth_invalid",
                )
            if self._proxy_context is None:
                try:
                    request_context = ProxyAttributionEnvelope.decode(
                        password,
                        self._credential_key,
                    )
                except OutboundDenied as exc:
                    raise BrowserNetworkDenied(
                        f"SECURITY_OUTBOUND_DENIED:{exc.code}",
                        code=exc.code,
                    ) from exc
            else:
                request_context = self._proxy_context
                try:
                    ProxyAttributionEnvelope.verify(
                        password,
                        self._credential_key,
                        request_context,
                    )
                except OutboundDenied as exc:
                    raise BrowserNetworkDenied(
                        f"SECURITY_OUTBOUND_DENIED:{exc.code}",
                        code=exc.code,
                    ) from exc
            parts = first.split(" ", 2)
            if len(parts) != 3:
                raise BrowserNetworkDenied("无效代理请求")
            method, target, version = parts
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise BrowserNetworkDenied("无效代理 HTTP 版本")
            if method.upper() == "CONNECT":
                hostname, port = self._split_authority(target, 443)
                audit_target = f"https://{hostname}:{port}"
                host_headers = [value for name, value in headers if name == "host"]
                if len(host_headers) != 1:
                    raise BrowserNetworkDenied("CONNECT Host 不明确")
                header_host, header_port = self._split_authority(host_headers[0], 443)
                if (
                    canonicalize_host(header_host) != canonicalize_host(hostname)
                    or header_port != port
                ):
                    raise BrowserNetworkDenied("CONNECT authority 与 Host 不一致")
                plan = await self.policy.plan_authority(
                    hostname,
                    port,
                    scheme="https",
                    method="CONNECT",
                    proxy_context=request_context,
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                response_started = True
                client_hello = await self._validated_tls_client_hello(
                    reader,
                    plan.target.host,
                )
                _remote_reader, remote_writer = await self._connect(plan)
                remote_writer.write(client_hello)
                await remote_writer.drain()
                await self._relay_bidirectional(reader, writer, _remote_reader, remote_writer)
                return

            _parsed, target_policy = self.policy.outbound.canonicalize_url(
                target,
                method=method,
                allowed_schemes=frozenset({"http", "ws"}),
            )
            audit_target = target_policy.audit_summary
            host_headers = [value for name, value in headers if name == "host"]
            if len(host_headers) != 1:
                raise BrowserNetworkDenied("代理 Host 不明确")
            header_host, header_port = self._split_authority(
                host_headers[0],
                80,
            )
            if (
                canonicalize_host(header_host) != target_policy.host
                or header_port != target_policy.port
            ):
                raise BrowserNetworkDenied("代理 target 与 Host 不一致")
            body_kind, body_length = self._request_body(headers)
            is_websocket = target_policy.scheme == "ws"
            if is_websocket:
                if (
                    method.upper() != "GET"
                    or body_kind != "none"
                    or not self._is_websocket_upgrade(headers)
                ):
                    raise BrowserNetworkDenied("无效 WebSocket upgrade 请求")
            elif any(name == "upgrade" for name, _value in headers):
                raise BrowserNetworkDenied("普通 HTTP 请求不允许协议升级")
            plan = await self.policy.plan_url(
                target_policy.canonical_url,
                method=method,
                allowed_schemes=frozenset({"http", "ws"}),
                proxy_context=request_context,
            )
            remote_reader, remote_writer = await self._connect(plan)
            path = plan.target.path or "/"
            if plan.target.query:
                path += f"?{plan.target.query}"
            forwarded_headers = self._forward_headers(
                headers,
                hostname=plan.target.host,
                port=plan.target.port,
                websocket=is_websocket,
            )
            remote_writer.write(
                (
                    f"{method} {path} {version}\r\n"
                    + "\r\n".join(forwarded_headers)
                    + "\r\n\r\n"
                ).encode("latin-1")
            )
            await remote_writer.drain()
            if is_websocket:
                response_head = await self._validated_websocket_response(remote_reader)
                writer.write(response_head)
                await writer.drain()
                # Do not relay any client bytes until the validated target has
                # completed a real WebSocket upgrade.  Before 101, pipelined
                # absolute-form bytes are still proxy requests and would
                # otherwise bypass per-request host policy.
                await self._relay_bidirectional(reader, writer, remote_reader, remote_writer)
                return

            if self._expects_continue(headers) and body_kind != "none":
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            await self._forward_request_body(
                reader,
                remote_writer,
                kind=body_kind,
                length=body_length,
            )
            # A forward-proxy TCP connection may carry multiple absolute-form
            # requests for different hosts.  Reusing the generic bidirectional
            # relay here would validate only the first host and then blindly
            # forward every later request on the socket.  Close the upstream
            # after one request and relay only its response; any pipelined or
            # keep-alive request remains unread and is discarded in ``finally``.
            await self._relay_http_response(remote_reader, writer, method=method)
        except (
            BrowserNetworkDenied,
            OutboundDenied,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValueError,
        ) as exc:
            reason = (
                exc.code
                if isinstance(exc, (BrowserNetworkDenied, OutboundDenied))
                else "invalid_proxy_request"
            )
            if not response_started:
                with suppress(Exception):
                    writer.write(
                        (
                            "HTTP/1.1 403 Forbidden\r\n"
                            f"X-Crew-Error-Code: {reason}\r\n"
                            "Content-Length: 0\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                    )
                    await writer.drain()
            log.info(
                "browser proxy blocked owner=%s target=%s reason=%s",
                self.policy.owner,
                audit_target,
                reason,
            )
        except Exception:
            log.exception("browser proxy connection failed")
        finally:
            if remote_writer is not None:
                remote_writer.close()
                with suppress(Exception):
                    await remote_writer.wait_closed()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
        raw = str(authority)
        if not raw or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
            raise BrowserNetworkDenied("CONNECT authority 无效")
        try:
            parsed = urlsplit(f"//{raw}")
            port = parsed.port or default_port
        except ValueError as exc:
            raise BrowserNetworkDenied("CONNECT authority 无效") from exc
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc != raw
        ):
            raise BrowserNetworkDenied("CONNECT 缺少主机名")
        return parsed.hostname, port

    @staticmethod
    def _parse_headers(header_lines: list[str]) -> list[tuple[str, str]]:
        headers: list[tuple[str, str]] = []
        for line in header_lines:
            if not line:
                continue
            if line[0] in " \t" or ":" not in line:
                raise BrowserNetworkDenied("无效代理请求头")
            name, value = line.split(":", 1)
            normalized = name.strip().lower()
            if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", normalized) is None:
                raise BrowserNetworkDenied("无效代理请求头名称")
            if any(
                ord(char) < 0x20 and char != "\t" or ord(char) == 0x7F
                for char in value
            ):
                raise BrowserNetworkDenied("无效代理请求头内容")
            headers.append((normalized, value.strip()))
        return headers

    def _request_body(self, headers: list[tuple[str, str]]) -> tuple[str, int]:
        content_lengths = [value for name, value in headers if name == "content-length"]
        transfer_encodings = [value for name, value in headers if name == "transfer-encoding"]
        if content_lengths and transfer_encodings:
            raise BrowserNetworkDenied("请求不能同时使用 Content-Length 和 Transfer-Encoding")
        if len(content_lengths) > 1 or len(transfer_encodings) > 1:
            raise BrowserNetworkDenied("请求体长度声明不明确")
        if content_lengths:
            value = content_lengths[0]
            if re.fullmatch(r"[0-9]+", value) is None:
                raise BrowserNetworkDenied("无效 Content-Length")
            length = int(value)
            if length > max(0, int(self.policy.config.max_transfer_bytes)):
                raise BrowserNetworkDenied("代理请求体超过大小限制")
            return ("content-length", length) if length else ("none", 0)
        if transfer_encodings:
            if transfer_encodings[0].strip().lower() != "chunked":
                raise BrowserNetworkDenied("仅支持 chunked Transfer-Encoding")
            return "chunked", 0
        return "none", 0

    @staticmethod
    def _is_websocket_upgrade(headers: list[tuple[str, str]]) -> bool:
        upgrades = [value.lower() for name, value in headers if name == "upgrade"]
        connection_tokens = {
            token.strip().lower()
            for name, value in headers
            if name == "connection"
            for token in value.split(",")
        }
        return upgrades == ["websocket"] and "upgrade" in connection_tokens

    async def _validated_tls_client_hello(
        self,
        reader: asyncio.StreamReader,
        expected_host: str,
    ) -> bytes:
        records = bytearray()
        handshake = bytearray()
        timeout = max(
            0.1,
            float(self.policy.config.command_timeout_seconds or 30),
        )
        deadline = asyncio.get_running_loop().time() + timeout

        async def read_exactly(length: int) -> bytes:
            return await self._read_with_deadline(
                reader,
                lambda: reader.readexactly(length),
                idle_timeout=timeout,
                deadline=deadline,
            )

        while len(records) <= 72 * 1024:
            header = await read_exactly(5)
            content_type = header[0]
            record_version = header[1:3]
            length = int.from_bytes(header[3:5], "big")
            if (
                content_type != 22
                or not record_version
                or record_version[0] != 3
                or length <= 0
                or length > 18 * 1024
            ):
                raise BrowserNetworkDenied("CONNECT 仅允许 TLS ClientHello")
            body = await read_exactly(length)
            records.extend(header)
            records.extend(body)
            handshake.extend(body)
            if len(handshake) < 4:
                continue
            if handshake[0] != 1:
                raise BrowserNetworkDenied("CONNECT 首个 TLS 消息不是 ClientHello")
            hello_length = int.from_bytes(handshake[1:4], "big")
            if hello_length <= 0 or hello_length > 64 * 1024:
                raise BrowserNetworkDenied("TLS ClientHello 过大")
            if len(handshake) < 4 + hello_length:
                continue
            sni = self._client_hello_sni(bytes(handshake[4 : 4 + hello_length]))
            try:
                canonical_expected = canonicalize_host(expected_host)
                canonical_sni = canonicalize_host(sni) if sni else ""
            except OutboundDenied as exc:
                raise BrowserNetworkDenied("TLS SNI 无效") from exc
            try:
                expected_is_ip = ipaddress.ip_address(canonical_expected) is not None
            except ValueError:
                expected_is_ip = False
            if canonical_sni:
                if canonical_sni != canonical_expected:
                    raise BrowserNetworkDenied("TLS SNI 与 CONNECT authority 不一致")
            elif not expected_is_ip:
                raise BrowserNetworkDenied("TLS ClientHello 缺少 SNI")
            return bytes(records)
        raise BrowserNetworkDenied("TLS ClientHello 过大")

    @staticmethod
    def _client_hello_sni(hello: bytes) -> str:
        def take(offset: int, length: int) -> tuple[bytes, int]:
            end = offset + length
            if length < 0 or end > len(hello):
                raise BrowserNetworkDenied("TLS ClientHello 结构无效")
            return hello[offset:end], end

        _fixed, offset = take(0, 34)
        session_length = hello[offset] if offset < len(hello) else -1
        _session, offset = take(offset + 1, session_length)
        cipher_length_bytes, offset = take(offset, 2)
        cipher_length = int.from_bytes(cipher_length_bytes, "big")
        if cipher_length < 2 or cipher_length % 2:
            raise BrowserNetworkDenied("TLS ClientHello cipher suites 无效")
        _ciphers, offset = take(offset, cipher_length)
        compression_length = hello[offset] if offset < len(hello) else -1
        _compression, offset = take(offset + 1, compression_length)
        if offset == len(hello):
            return ""
        extensions_length_bytes, offset = take(offset, 2)
        extensions_length = int.from_bytes(extensions_length_bytes, "big")
        extensions, offset = take(offset, extensions_length)
        if offset != len(hello):
            raise BrowserNetworkDenied("TLS ClientHello extensions 边界无效")

        cursor = 0
        while cursor < len(extensions):
            if cursor + 4 > len(extensions):
                raise BrowserNetworkDenied("TLS ClientHello extension 无效")
            extension_type = int.from_bytes(extensions[cursor : cursor + 2], "big")
            extension_length = int.from_bytes(extensions[cursor + 2 : cursor + 4], "big")
            cursor += 4
            end = cursor + extension_length
            if end > len(extensions):
                raise BrowserNetworkDenied("TLS ClientHello extension 越界")
            data = extensions[cursor:end]
            cursor = end
            if extension_type != 0:
                continue
            if len(data) < 5:
                raise BrowserNetworkDenied("TLS SNI extension 无效")
            names_length = int.from_bytes(data[:2], "big")
            if names_length != len(data) - 2:
                raise BrowserNetworkDenied("TLS SNI 列表边界无效")
            name_type = data[2]
            name_length = int.from_bytes(data[3:5], "big")
            if name_type != 0 or name_length <= 0 or 5 + name_length != len(data):
                raise BrowserNetworkDenied("TLS SNI 名称无效")
            try:
                return data[5:].decode("ascii")
            except UnicodeDecodeError as exc:
                raise BrowserNetworkDenied("TLS SNI 不是 ASCII") from exc
        return ""

    async def _validated_websocket_response(self, reader: asyncio.StreamReader) -> bytes:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        if len(head) > 64 * 1024:
            raise BrowserNetworkDenied("WebSocket 响应头过大")
        first, *header_lines = head.decode("latin-1", errors="replace").split("\r\n")
        parts = first.split(" ", 2)
        if len(parts) < 2 or parts[0].upper() != "HTTP/1.1" or parts[1] != "101":
            raise BrowserNetworkDenied("上游未接受 WebSocket upgrade")
        headers = self._parse_headers(header_lines)
        if not self._is_websocket_upgrade(headers):
            raise BrowserNetworkDenied("上游返回了无效 WebSocket upgrade")
        return head

    @staticmethod
    def _expects_continue(headers: list[tuple[str, str]]) -> bool:
        expectations = [value.lower() for name, value in headers if name == "expect"]
        if not expectations:
            return False
        if expectations != ["100-continue"]:
            raise BrowserNetworkDenied("不支持的 Expect 请求头")
        return True

    @staticmethod
    def _forward_headers(
        headers: list[tuple[str, str]],
        *,
        hostname: str,
        port: int,
        websocket: bool,
    ) -> list[str]:
        connection_tokens = {
            token.strip().lower()
            for name, value in headers
            if name == "connection"
            for token in value.split(",")
        }
        removed = {"host", "proxy-authorization", "proxy-connection", "expect"}
        if not websocket:
            removed.update({"connection", "keep-alive", "upgrade"})
            removed.update(connection_tokens)
        forwarded = [
            f"{name}: {value}"
            for name, value in headers
            if name not in removed and not name.startswith("proxy-")
        ]
        display_host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 80
        host_header = display_host if port == default_port else f"{display_host}:{port}"
        forwarded.append(f"Host: {host_header}")
        if not websocket:
            forwarded.append("Connection: close")
        return forwarded

    async def _forward_request_body(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        kind: str,
        length: int,
    ) -> None:
        if kind == "none":
            return
        timeout = max(
            0.1,
            float(self.policy.config.command_timeout_seconds or 30),
        )
        deadline = asyncio.get_running_loop().time() + timeout

        async def bounded(operation):
            return await self._read_with_deadline(
                reader,
                operation,
                idle_timeout=timeout,
                deadline=deadline,
            )

        if kind == "content-length":
            remaining = length
            while remaining:
                amount = min(remaining, 64 * 1024)
                chunk = await bounded(
                    lambda amount=amount: reader.readexactly(amount)
                )
                writer.write(chunk)
                await bounded(writer.drain)
                remaining -= len(chunk)
            return

        trailer_bytes = 0
        body_bytes = 0
        while True:
            line = await bounded(lambda: reader.readuntil(b"\r\n"))
            if len(line) > 8192:
                raise BrowserNetworkDenied("chunked 请求体块头过大")
            raw_size = line[:-2].split(b";", 1)[0].strip()
            if re.fullmatch(rb"[0-9a-fA-F]+", raw_size) is None:
                raise BrowserNetworkDenied("无效 chunked 请求体")
            size = int(raw_size, 16)
            body_bytes += size
            if body_bytes > max(0, int(self.policy.config.max_transfer_bytes)):
                raise BrowserNetworkDenied("chunked 请求体超过大小限制")
            writer.write(line)
            if size:
                data = await bounded(lambda: reader.readexactly(size + 2))
                if not data.endswith(b"\r\n"):
                    raise BrowserNetworkDenied("无效 chunked 请求体分隔符")
                writer.write(data)
                await bounded(writer.drain)
                continue

            while True:
                trailer = await bounded(lambda: reader.readuntil(b"\r\n"))
                trailer_bytes += len(trailer)
                if trailer_bytes > 64 * 1024:
                    raise BrowserNetworkDenied("chunked 请求体 trailer 过大")
                if trailer == b"\r\n":
                    writer.write(trailer)
                    break
                raise BrowserNetworkDenied("chunked 请求体 trailer 不受支持")
            await bounded(writer.drain)
            return

    def _response_timeouts(self) -> tuple[float, float]:
        idle = max(0.1, float(self.policy.config.command_timeout_seconds or 30))
        total = max(idle, float(self.policy.config.navigation_timeout_seconds or 60))
        return idle, total

    @staticmethod
    async def _read_with_deadline(
        reader: asyncio.StreamReader,
        operation,
        *,
        idle_timeout: float,
        deadline: float,
    ):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        return await asyncio.wait_for(operation(), timeout=min(idle_timeout, remaining))

    def _response_body(
        self,
        headers: list[tuple[str, str]],
        *,
        method: str,
        status: int,
    ) -> tuple[str, int]:
        if method.upper() == "HEAD" or status in {204, 205, 304}:
            return "none", 0
        content_lengths = [value for name, value in headers if name == "content-length"]
        transfer_encodings = [value for name, value in headers if name == "transfer-encoding"]
        if content_lengths and transfer_encodings:
            raise BrowserNetworkDenied("上游响应体边界不明确")
        if len(content_lengths) > 1:
            raise BrowserNetworkDenied("上游返回了多个 Content-Length")
        if transfer_encodings:
            tokens = [
                token.strip().lower()
                for value in transfer_encodings
                for token in value.split(",")
            ]
            if not tokens or tokens[-1] != "chunked" or tokens.count("chunked") != 1:
                raise BrowserNetworkDenied("上游响应使用了不支持的 Transfer-Encoding")
            return "chunked", 0
        if content_lengths:
            value = content_lengths[0]
            if re.fullmatch(r"[0-9]+", value) is None:
                raise BrowserNetworkDenied("上游返回了无效 Content-Length")
            length = int(value)
            if length > max(0, int(self.policy.config.max_transfer_bytes)):
                raise BrowserNetworkDenied("上游响应体超过大小限制")
            return ("content-length", length) if length else ("none", 0)
        return "close", 0

    async def _relay_http_response(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        method: str,
    ) -> None:
        idle_timeout, total_timeout = self._response_timeouts()
        deadline = asyncio.get_running_loop().time() + total_timeout
        sent_informational = False
        for _index in range(16):
            try:
                head = await self._read_with_deadline(
                    reader,
                    lambda: reader.readuntil(b"\r\n\r\n"),
                    idle_timeout=idle_timeout,
                    deadline=deadline,
                )
            except (TimeoutError, asyncio.IncompleteReadError):
                if sent_informational:
                    return
                raise
            if len(head) > 64 * 1024:
                raise BrowserNetworkDenied("上游响应头过大")
            first, *header_lines = head.decode("latin-1", errors="replace").split("\r\n")
            match = re.fullmatch(r"HTTP/(?:1\.0|1\.1) ([0-9]{3})(?: .*)?", first)
            if match is None:
                raise BrowserNetworkDenied("上游返回了无效 HTTP 响应")
            status = int(match.group(1))
            if status < 100 or status > 599:
                raise BrowserNetworkDenied("上游返回了无效 HTTP 状态码")
            headers = self._parse_headers(header_lines)
            if 100 <= status < 200:
                if status == 101:
                    raise BrowserNetworkDenied("普通 HTTP 响应不允许协议升级")
                writer.write(head)
                await writer.drain()
                sent_informational = True
                continue

            body_kind, body_length = self._response_body(
                headers,
                method=method,
                status=status,
            )
            writer.write(head)
            await writer.drain()
            try:
                await self._relay_response_body(
                    reader,
                    writer,
                    kind=body_kind,
                    length=body_length,
                    idle_timeout=idle_timeout,
                    deadline=deadline,
                )
            except (
                BrowserNetworkDenied,
                TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ) as exc:
                # The final response head may already be visible to Chromium.
                # Closing it is the only unambiguous failure signal; appending a
                # proxy-generated 403 would corrupt the response framing.
                log.info("browser proxy truncated invalid upstream response: %s", type(exc).__name__)
            return
        raise BrowserNetworkDenied("上游返回了过多 informational 响应")

    async def _relay_response_body(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        kind: str,
        length: int,
        idle_timeout: float,
        deadline: float,
    ) -> None:
        if kind == "none":
            return
        if kind == "content-length":
            remaining = length
            while remaining:
                amount = min(remaining, 64 * 1024)
                chunk = await self._read_with_deadline(
                    reader,
                    lambda amount=amount: reader.readexactly(amount),
                    idle_timeout=idle_timeout,
                    deadline=deadline,
                )
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)
            return
        if kind == "chunked":
            await self._relay_chunked_response(
                reader,
                writer,
                idle_timeout=idle_timeout,
                deadline=deadline,
            )
            return

        received = 0
        while True:
            chunk = await self._read_with_deadline(
                reader,
                lambda: reader.read(64 * 1024),
                idle_timeout=idle_timeout,
                deadline=deadline,
            )
            if not chunk:
                return
            received += len(chunk)
            if received > max(0, int(self.policy.config.max_transfer_bytes)):
                raise BrowserNetworkDenied("上游响应体超过大小限制")
            writer.write(chunk)
            await writer.drain()

    async def _relay_chunked_response(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        idle_timeout: float,
        deadline: float,
    ) -> None:
        trailer_bytes = 0
        body_bytes = 0
        while True:
            line = await self._read_with_deadline(
                reader,
                lambda: reader.readuntil(b"\r\n"),
                idle_timeout=idle_timeout,
                deadline=deadline,
            )
            if len(line) > 8192:
                raise BrowserNetworkDenied("上游 chunked 响应块头过大")
            raw_size = line[:-2].split(b";", 1)[0].strip()
            if re.fullmatch(rb"[0-9a-fA-F]+", raw_size) is None:
                raise BrowserNetworkDenied("上游返回了无效 chunked 响应")
            size = int(raw_size, 16)
            body_bytes += size
            if body_bytes > max(0, int(self.policy.config.max_transfer_bytes)):
                raise BrowserNetworkDenied("上游 chunked 响应超过大小限制")
            writer.write(line)
            if size:
                amount = size + 2
                data = await self._read_with_deadline(
                    reader,
                    lambda amount=amount: reader.readexactly(amount),
                    idle_timeout=idle_timeout,
                    deadline=deadline,
                )
                if not data.endswith(b"\r\n"):
                    raise BrowserNetworkDenied("上游 chunked 响应分隔符无效")
                writer.write(data)
                await writer.drain()
                continue

            while True:
                trailer = await self._read_with_deadline(
                    reader,
                    lambda: reader.readuntil(b"\r\n"),
                    idle_timeout=idle_timeout,
                    deadline=deadline,
                )
                trailer_bytes += len(trailer)
                if trailer_bytes > 64 * 1024:
                    raise BrowserNetworkDenied("上游 chunked 响应 trailer 过大")
                writer.write(trailer)
                if trailer == b"\r\n":
                    break
            await writer.drain()
            return

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        transferred = 0
        try:
            while data := await reader.read(64 * 1024):
                transferred += len(data)
                if transferred > max(0, int(self.policy.config.max_transfer_bytes)):
                    raise BrowserNetworkDenied("代理隧道超过传输大小限制")
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError, BrowserNetworkDenied):
            pass

    async def _relay_bidirectional(
        self,
        left_reader: asyncio.StreamReader,
        left_writer: asyncio.StreamWriter,
        right_reader: asyncio.StreamReader,
        right_writer: asyncio.StreamWriter,
    ) -> None:
        tasks = {
            asyncio.create_task(self._pipe(left_reader, right_writer)),
            asyncio.create_task(self._pipe(right_reader, left_writer)),
        }
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def path_is_within(path, roots: Iterable) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
