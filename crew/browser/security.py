"""Network compatibility helpers and authenticated loopback proxy."""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import re
import secrets
import socket
from contextlib import suppress
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from crew.browser.types import BrowserConfig
from crew.state.logging import get_logger

log = get_logger("browser.security")


class BrowserNetworkDenied(ValueError):
    pass


_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("168.63.129.16"),
        ipaddress.ip_address("169.254.169.253"),
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)
_NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _embedded_ipv4(value: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, ...]:
    candidates: list[ipaddress.IPv4Address] = []
    if value.ipv4_mapped is not None:
        candidates.append(value.ipv4_mapped)
    if value.sixtofour is not None:
        candidates.append(value.sixtofour)
    if value.teredo is not None:
        candidates.extend(value.teredo)
    if any(value in network for network in _NAT64_PREFIXES) or int(value) >> 32 == 0:
        candidates.append(ipaddress.IPv4Address(int(value) & 0xFFFFFFFF))
    return tuple(dict.fromkeys(candidates))


def _is_sensitive_address(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if value in _METADATA_ADDRESSES or not value.is_global or getattr(value, "is_site_local", False):
        return True
    if isinstance(value, ipaddress.IPv6Address):
        return any(_is_sensitive_address(item) for item in _embedded_ipv4(value))
    return False


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


@dataclass(frozen=True)
class ResolvedTarget:
    hostname: str
    ip: str
    port: int


class BrowserNetworkPolicy:
    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._blocked_hosts = {_normalized_host(item) for item in config.blocked_hosts}
        self._allowed_private_hosts = {
            _normalized_host(item) for item in config.allowed_private_hosts
        }
        try:
            self._allowed_private_networks = tuple(
                ipaddress.ip_network(item, strict=False)
                for item in config.allowed_private_cidrs
            )
        except ValueError as exc:
            raise BrowserNetworkDenied("allowed_private_cidrs 包含无效网段") from exc

    def validate_navigation_url(self, url: str) -> str:
        """Validate direct numeric network targets before they reach Chromium.

        DNS names are resolved and pinned by ``LoopbackPolicyProxy``. Non-network
        browser schemes retain Chromium semantics and are governed by the Browser
        Host's separate artifact/file boundaries.
        """
        raw = str(url or "")
        if not raw.strip():
            raise BrowserNetworkDenied("URL 不能为空")
        parsed = urlsplit(raw)
        if parsed.scheme.lower() in {"http", "https", "ws", "wss"} and parsed.hostname:
            host = self.validate_hostname(parsed.hostname)
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                self.validate_ip(host, host)
        return raw

    def validate_hostname(self, hostname: str) -> str:
        host = _normalized_host(hostname)
        if not host:
            raise BrowserNetworkDenied("主机名不能为空")
        if host in self._blocked_hosts or any(host.endswith(f".{item}") for item in self._blocked_hosts):
            raise BrowserNetworkDenied("该主机已被管理员策略阻止")
        return host

    def validate_ip(self, hostname: str, value: str) -> str:
        host = self.validate_hostname(hostname)
        try:
            ip = ipaddress.ip_address(value)
        except ValueError as exc:
            raise BrowserNetworkDenied("DNS 返回了无效 IP") from exc
        host_allowed = host in self._allowed_private_hosts or any(
            host.endswith(f".{item}") for item in self._allowed_private_hosts
        )
        cidr_allowed = any(ip in network for network in self._allowed_private_networks)
        if not host_allowed and not cidr_allowed and _is_sensitive_address(ip):
            raise BrowserNetworkDenied("默认禁止访问本机、私网或云元数据地址")
        return str(ip)

    async def resolve(self, hostname: str, port: int) -> ResolvedTarget:
        candidates = await self.resolve_candidates(hostname, port)
        return candidates[0]

    async def resolve_candidates(self, hostname: str, port: int) -> list[ResolvedTarget]:
        host = self.validate_hostname(hostname)
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        denied: BrowserNetworkDenied | None = None
        candidates: list[ResolvedTarget] = []
        seen: set[str] = set()
        for _family, _type, _proto, _canon, sockaddr in infos:
            value = str(sockaddr[0])
            try:
                ip = self.validate_ip(host, value)
            except BrowserNetworkDenied as exc:
                denied = exc
                continue
            if ip in seen:
                continue
            seen.add(ip)
            candidates.append(ResolvedTarget(host, ip, int(port)))
        if candidates:
            return candidates
        raise denied or BrowserNetworkDenied("DNS 未返回可连接地址")


class LoopbackPolicyProxy:
    """Authenticated policy proxy that resolves, validates and pins every target."""

    def __init__(self, policy: BrowserNetworkPolicy) -> None:
        self.policy = policy
        self._server: asyncio.AbstractServer | None = None
        self._username = "crew"
        self._password = secrets.token_urlsafe(32)

    @property
    def url(self) -> str:
        if self._server is None or not self._server.sockets:
            return ""
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://{self._username}:{self._password}@127.0.0.1:{port}"

    async def start(self) -> str:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.url

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    _CONNECT_TIMEOUT = 10.0

    async def _connect(
        self, candidates: list[ResolvedTarget]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to the first reachable validated address.

        DNS often returns an unreachable IPv6 address before a working IPv4
        one (no happy-eyeballs in a pinning proxy). Try every validated
        candidate in order with a short per-attempt timeout so a black-holed
        address fails fast instead of stalling on the kernel TCP timeout.
        """
        last_error: BaseException | None = None
        for candidate in candidates:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(candidate.ip, candidate.port),
                    timeout=self._CONNECT_TIMEOUT,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                log.info(
                    "browser proxy upstream %s:%s unreachable: %s",
                    candidate.ip,
                    candidate.port,
                    exc,
                )
                last_error = exc
        assert last_error is not None  # resolve_candidates never returns empty
        raise last_error

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        remote_writer: asyncio.StreamWriter | None = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(head) > 64 * 1024:
                raise BrowserNetworkDenied("代理请求头过大")
            first, *header_lines = head.decode("latin-1", errors="replace").split("\r\n")
            authorization = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in header_lines
                    if line.lower().startswith("proxy-authorization:")
                ),
                "",
            )
            expected = "Basic " + base64.b64encode(
                f"{self._username}:{self._password}".encode("utf-8")
            ).decode("ascii")
            if not hmac.compare_digest(authorization, expected):
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                    b'Proxy-Authenticate: Basic realm="Crew Browser"\r\n'
                    b'Content-Length: 0\r\nConnection: close\r\n\r\n'
                )
                await writer.drain()
                return
            parts = first.split(" ", 2)
            if len(parts) != 3:
                raise BrowserNetworkDenied("无效代理请求")
            method, target, version = parts
            if method.upper() == "CONNECT":
                hostname, port = self._split_authority(target, 443)
                candidates = await self.policy.resolve_candidates(hostname, port)
                _remote_reader, remote_writer = await self._connect(candidates)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._relay_bidirectional(reader, writer, _remote_reader, remote_writer)
                return

            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "ws"} or not parsed.hostname:
                raise BrowserNetworkDenied("HTTP 代理仅接受绝对 http/ws URL")
            port = parsed.port or 80
            candidates = await self.policy.resolve_candidates(parsed.hostname, port)
            headers = self._parse_headers(header_lines)
            body_kind, body_length = self._request_body(headers)
            is_websocket = parsed.scheme == "ws"
            if is_websocket:
                if (
                    method.upper() != "GET"
                    or body_kind != "none"
                    or not self._is_websocket_upgrade(headers)
                ):
                    raise BrowserNetworkDenied("无效 WebSocket upgrade 请求")
            elif any(name == "upgrade" for name, _value in headers):
                raise BrowserNetworkDenied("普通 HTTP 请求不允许协议升级")
            remote_reader, remote_writer = await self._connect(candidates)
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            forwarded_headers = self._forward_headers(
                headers,
                hostname=candidates[0].hostname,
                port=port,
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
        except (BrowserNetworkDenied, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
            with suppress(Exception):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            log.info("browser proxy blocked request: %s", type(exc).__name__)
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
        parsed = urlsplit(f"//{authority}")
        if not parsed.hostname:
            raise BrowserNetworkDenied("CONNECT 缺少主机名")
        return parsed.hostname, parsed.port or default_port

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
            if "\r" in value or "\n" in value:
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
        if kind == "content-length":
            remaining = length
            while remaining:
                chunk = await reader.readexactly(min(remaining, 64 * 1024))
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)
            return

        trailer_bytes = 0
        while True:
            line = await reader.readuntil(b"\r\n")
            if len(line) > 8192:
                raise BrowserNetworkDenied("chunked 请求体块头过大")
            raw_size = line[:-2].split(b";", 1)[0].strip()
            if re.fullmatch(rb"[0-9a-fA-F]+", raw_size) is None:
                raise BrowserNetworkDenied("无效 chunked 请求体")
            size = int(raw_size, 16)
            writer.write(line)
            if size:
                data = await reader.readexactly(size + 2)
                if not data.endswith(b"\r\n"):
                    raise BrowserNetworkDenied("无效 chunked 请求体分隔符")
                writer.write(data)
                await writer.drain()
                continue

            while True:
                trailer = await reader.readuntil(b"\r\n")
                trailer_bytes += len(trailer)
                if trailer_bytes > 64 * 1024:
                    raise BrowserNetworkDenied("chunked 请求体 trailer 过大")
                writer.write(trailer)
                if trailer == b"\r\n":
                    break
            await writer.drain()
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

        while True:
            chunk = await self._read_with_deadline(
                reader,
                lambda: reader.read(64 * 1024),
                idle_timeout=idle_timeout,
                deadline=deadline,
            )
            if not chunk:
                return
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

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(64 * 1024):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
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
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def path_is_within(path, roots: Iterable) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
